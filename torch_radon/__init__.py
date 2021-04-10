import numpy as np
import torch
import scipy.stats
import abc
import torch.nn.functional as F
from typing import Union

try:
    import torch_radon_cuda
except Exception as e:
    print("Error importing torch_radon_cuda:", e)

from .differentiable_functions import RadonForward, RadonBackprojection
from .utils import normalize_shape, ShapeNormalizer
from .filtering import FourierFilters
from .parameter_classes import Volume, Projection, ExecCfg
from .log_levels import *

__version__ = "2.0"




class ExecCfgGeneratorBase:
    def __init__(self):
        pass

    def __call__(self, vol_cfg, proj_cfg, is_half):
        if proj_cfg.projection_type == 2:
            ch = 4 if is_half else 1
            return ExecCfg(8, 16, 8, ch)

        return ExecCfg(16, 16, 1, 4)


class Radon:
    def __init__(self, angles: Union[list, np.array, torch.Tensor], volume: Union[int, tuple, Volume],
                 projection: Projection = None):
        # make sure that angles are a PyTorch tensor
        if not isinstance(angles, torch.Tensor):
            angles = torch.FloatTensor(angles)

        if isinstance(volume, int):
            volume = Volume.create_2d(volume, volume)
        elif isinstance(volume, tuple):
            volume = Volume.create_2d(*volume) if len(volume) == 2 else Volume.create_3d(*volume)

        if projection is None:
            projection = Projection.parallel_beam(volume.max_dim())

        self.angles = angles
        self.volume = volume
        self.projection = projection
        self.exec_cfg_generator = ExecCfgGeneratorBase()

        # caches used to avoid reallocation of resources
        self.tex_cache = torch_radon_cuda.TextureCache(8)
        self.fft_cache = torch_radon_cuda.FFTCache(8)
        self.fourier_filters = FourierFilters()

        seed = np.random.get_state()[1][0]
        self.noise_generator = torch_radon_cuda.RadonNoiseGenerator(seed)

    def _move_parameters_to_device(self, device):
        if device != self.angles.device:
            self.angles = self.angles.to(device)

    def _check_input(self, x):
        if not x.is_contiguous():
            x = x.contiguous()

        if x.dtype == torch.float16:
            assert x.size(
                0) % 4 == 0, f"Batch size must be multiple of 4 when using half precision. Got batch size {x.size(0)}"

        return x

    def forward(self, x: torch.Tensor, angles: torch.Tensor = None, exec_cfg: ExecCfg = None):
        r"""Radon forward projection.

        :param x: PyTorch GPU tensor.
        :param angles: PyTorch GPU tensor indicating the measuring angles, if None the angles given to the constructor
        are used
        :returns: PyTorch GPU tensor containing sinograms.
        """
        x = self._check_input(x)
        self._move_parameters_to_device(x.device)

        angles = angles if angles is not None else self.angles

        shape_normalizer = ShapeNormalizer(self.volume.num_dimensions())
        x = shape_normalizer.normalize(x)

        self.projection.cfg.n_angles = len(angles)

        y = RadonForward.apply(x, self.angles, self.tex_cache, self.volume.cfg, self.projection.cfg,
                               self.exec_cfg_generator, exec_cfg)

        return shape_normalizer.unnormalize(y)

    def backprojection(self, sinogram, angles: torch.Tensor = None, exec_cfg: ExecCfg = None):
        r"""Radon backward projection.

        :param sinogram: PyTorch GPU tensor containing sinograms.
        :param angles: PyTorch GPU tensor indicating the measuring angles, if None the angles given to the constructor
        are used
        :returns: PyTorch GPU tensor containing backprojected volume.
        """
        sinogram = self._check_input(sinogram)
        self._move_parameters_to_device(sinogram.device)

        angles = angles if angles is not None else self.angles

        shape_normalizer = ShapeNormalizer(self.volume.num_dimensions())
        sinogram = shape_normalizer.normalize(sinogram)

        self.projection.cfg.n_angles = len(angles)

        y = RadonBackprojection.apply(sinogram, self.angles, self.tex_cache, self.volume.cfg, self.projection.cfg,
                                      self.exec_cfg_generator, exec_cfg)

        return shape_normalizer.unnormalize(y)

    def backward(self, sinogram):
        r"""Radon backward projection.

        :param sinogram: PyTorch GPU tensor containing sinograms.
        :returns: PyTorch GPU tensor containing backprojected volume.
        """
        return self.backprojection(sinogram)

    @normalize_shape(2)
    def filter_sinogram(self, sinogram, filter_name="ramp"):
        size = sinogram.size(2)
        n_angles = sinogram.size(1)

        # Pad sinogram to improve accuracy
        padded_size = max(64, int(2 ** np.ceil(np.log2(2 * size))))
        pad = padded_size - size
        padded_sinogram = F.pad(sinogram.float(), (0, pad, 0, 0))
        print(padded_sinogram.size())

        # sino_fft = torch.fft.rfft(padded_sinogram, norm="ortho")
        sino_fft = torch_radon_cuda.rfft(padded_sinogram, self.fft_cache)

        # get filter and apply
        f = self.fourier_filters.get(padded_size, "ramp", sinogram.device)
        print("fft", sino_fft.size(), f.size())
        filtered_sino_fft = sino_fft * f
        print(filtered_sino_fft.size())

        # Inverse fft
        filtered_sinogram = torch_radon_cuda.irfft(filtered_sino_fft, self.fft_cache)
        filtered_sinogram = filtered_sinogram[:, :, :-pad] * (np.pi / (2 * n_angles * padded_size))

        return filtered_sinogram.to(dtype=sinogram.dtype)


    @normalize_shape(2)
    def add_noise(self, x, signal, density_normalization=1.0, approximate=False):
        # print("WARN Radon.add_noise is deprecated")

        torch_radon_cuda.add_noise(x, self.noise_generator, signal, density_normalization, approximate)
        return x

    @normalize_shape(2)
    def emulate_readings(self, x, signal, density_normalization=1.0):
        return torch_radon_cuda.emulate_sensor_readings(x, self.noise_generator, signal, density_normalization)

    @normalize_shape(2)
    def emulate_readings_new(self, x, signal, normal_std, k, bins):
        return torch_radon_cuda.emulate_readings_new(x, self.noise_generator, signal, normal_std, k, bins)

    @normalize_shape(2)
    def readings_lookup(self, sensor_readings, lookup_table):
        return torch_radon_cuda.readings_lookup(sensor_readings, lookup_table)


    def set_seed(self, seed=-1):
        if seed < 0:
            seed = np.random.get_state()[1][0]

        self.noise_generator.set_seed(seed)

    def __del__(self):
        self.noise_generator.free()



def compute_lookup_table(sinogram, signal, normal_std, bins=4096, eps=0.01, eps_prob=0.99, eps_k=0.01, verbose=False):
    s = sinogram.view(-1)
    device = s.device

    eps = np.quantile(sinogram.cpu().numpy(), eps) + eps_k

    # Compute readings normalization value
    if verbose:
        print("Computing readings normalization value")
    k = 0
    for i in range(1, 5000):
        a, b = torch_radon_cuda.compute_ab(s, signal, eps, bins * i)
        if verbose:
            print(a, b)
        if a >= (a + b) * eps_prob:
            k = bins * i
            break
    print("Readings normalization value = ", k // bins)

    # Compute weights for Gaussian error
    scale = k // bins
    weights = []
    for i in range(0, 64):
        t = scipy.stats.norm.cdf((scale - i - 0.5) / normal_std) - scipy.stats.norm.cdf((- i - 0.5) / normal_std)
        if t < 0.005:
            break

        weights.append(t)

    weights = weights[scale:][::-1] + weights
    weights = np.array(weights)

    border_w = np.asarray([scipy.stats.norm.cdf((-x - 0.5) / normal_std) for x in range(scale)])
    border_w = torch.FloatTensor(border_w).to(device)

    log_factorial = np.arange(k + len(weights))
    log_factorial[0] = 1
    log_factorial = np.cumsum(np.log(log_factorial).astype(np.float64)).astype(np.float32)
    log_factorial = torch.Tensor(log_factorial).to(device)

    weights = torch.FloatTensor(weights).to(device)

    lookup, lookup_var = torch_radon_cuda.compute_lookup_table(s, weights, signal, bins, scale, log_factorial, border_w)

    return lookup, lookup_var, scale


class ReadingsLookup:
    def __init__(self, radon, bins=4096, mu=None, sigma=None, ks=None, signals=None, normal_stds=None):
        self.radon = radon
        self.bins = bins

        self.mu = [] if mu is None else mu
        self.sigma = [] if sigma is None else sigma
        self.ks = [] if ks is None else ks

        self.signals = [] if signals is None else signals
        self.normal_stds = [] if normal_stds is None else normal_stds

        self._mu = None
        self._sigma = None
        self._ks = None
        self._signals = None
        self._normal_stds = None
        self._need_repacking = True

    def repack(self, device):
        self._mu = torch.FloatTensor(self.mu).to(device)
        self._sigma = torch.FloatTensor(self.sigma).to(device)
        self._ks = torch.IntTensor(self.ks).to(device)
        self._signals = torch.FloatTensor(self.signals).to(device)
        self._normal_stds = torch.FloatTensor(self.normal_stds).to(device)

    @staticmethod
    def from_file(path, radon):
        obj = np.load(path)

        bins = int(obj["bins"])

        return ReadingsLookup(radon, bins, list(obj["mu"]), list(obj["sigma"]), list(obj["ks"]), list(obj["signals"]),
                              list(obj["normal_stds"]))

    def save(self, path):
        self.repack("cpu")
        np.savez(path, mu=self._mu, sigma=self._sigma, ks=self._ks, signals=self._signals,
                 normal_stds=self._normal_stds, bins=self.bins)

    def add_lookup_table(self, sinogram, signal, normal_std, eps=0.01, eps_prob=0.99, eps_k=0.01, verbose=True):
        lookup, lookup_var, k = compute_lookup_table(sinogram, signal, normal_std, self.bins, eps, eps_prob, eps_k,
                                                     verbose)

        self.mu.append(lookup.cpu().numpy())
        self.sigma.append(lookup_var.cpu().numpy())
        self.ks.append(k)
        self.signals.append(signal)
        self.normal_stds.append(normal_std)
        self._need_repacking = True

    @normalize_shape(2)
    def emulate_readings(self, sinogram, level):
        if self._need_repacking or self._mu.device != sinogram.device:
            self.repack(sinogram.device)

        if isinstance(level, torch.Tensor):
            return torch_radon_cuda.emulate_readings_multilevel(sinogram, self.radon.noise_generator, self._signals,
                                                                self._normal_stds, self._ks, level, self.bins)
        else:
            return torch_radon_cuda.emulate_readings_new(sinogram, self.radon.noise_generator, self.signals[level],
                                                         self.normal_stds[level], self.ks[level], self.bins)

    @normalize_shape(2)
    def lookup(self, readings, level):
        if self._need_repacking or self._mu.device != readings.device:
            self.repack(readings.device)

        if isinstance(level, torch.Tensor):
            mu = torch_radon_cuda.readings_lookup_multilevel(readings, self._mu, level)
            sigma = torch_radon_cuda.readings_lookup_multilevel(readings, self._sigma, level)
        else:
            mu = torch_radon_cuda.readings_lookup(readings, self._mu[level])
            sigma = torch_radon_cuda.readings_lookup(readings, self._sigma[level])

        return mu, sigma

    def random_levels(self, size, device):
        return torch.randint(0, len(self.mu), (size,), device=device, dtype=torch.int32)
