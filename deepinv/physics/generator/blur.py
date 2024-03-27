# %%
import torch
from typing import List, Tuple
import numpy as np
from deepinv.physics.generator import PhysicsGenerator
from math import ceil, floor
from deepinv.physics.functional import histogramdd


class PSFGenerator(PhysicsGenerator):
    def __init__(
        self,
        shape: tuple,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.shape = shape
        kernel_size = (self.shape[-2], self.shape[-1])

        self.kernel_size = kernel_size
        # The default Dirac mass (identity)
        self.dirac_mass = torch.zeros(
            (1, 1, kernel_size[0], kernel_size[1]), **self.factory_kwargs
        )
        self.dirac_mass[..., kernel_size[0] // 2, kernel_size[1] // 2] = 1.0


class DiffractionBlurGenerator(PSFGenerator):
    r"""
    Diffraction limited blur generator.

    Generates 2D diffraction kernels in optics using Zernike decomposition of the phase mask (Fresnel/Fraunhoffer diffraction theory)

    :param tuple shape:
    :param list[str] list_param: list of activated Zernike coefficients, defaults to `["Z4", "Z5", "Z6","Z7", "Z8", "Z9", "Z10", "Z11"]`
    :param float fc: cutoff frequency (NA/emission_wavelength) * pixel_size. Should be in `[0, 1/4]` to respect Shannon, defaults to `0.2`
    :param tuple[int] pupil_size: this is used to synthesize the super-resolved pupil. The higher the more precise, defaults to (256, 256).
            If a int is given, a square pupil is considered.
    :param float max_zernike_amplitude: range for the Zernike coefficient linear combination, defaults to 0.2.

    :return: a DiffractionBlurGenerator object

    |sep|

    :Examples:

    >>> generator = DiffractionBlurGenerator((16, 16))
    >>> filter = generator.step()
    >>> dinv.utils.plot(filter)
    >>> print(filter.shape)
    torch.Size([1, 1, 16, 16])

    """

    def __init__(
        self,
        shape: tuple,
        device: str = "cpu",
        dtype: type = torch.float32,
        list_param: List[str] = ["Z4", "Z5", "Z6", "Z7", "Z8", "Z9", "Z10", "Z11"],
        fc: float = 0.2,
        max_zernike_amplitude: float = 0.15,
        pupil_size: Tuple[int] = (256, 256),
    ):
        kwargs = {"list_param": list_param, "fc": fc, "pupil_size": pupil_size}
        super().__init__(shape=shape, device=device, dtype=dtype, **kwargs)

        self.list_param = list_param  # list of parameters to provide

        pupil_size = (
            max(self.pupil_size[0], self.kernel_size[0]),
            max(self.pupil_size[1], self.kernel_size[1]),
        )
        self.pupil_size = pupil_size

        lin_x = torch.linspace(-0.5, 0.5, self.pupil_size[0], **self.factory_kwargs)
        lin_y = torch.linspace(-0.5, 0.5, self.pupil_size[1], **self.factory_kwargs)

        # Fourier plane is discretized on [-0.5,0.5]x[-0.5,0.5]
        XX, YY = torch.meshgrid(lin_x / self.fc, lin_y / self.fc, indexing="ij")
        self.rho = cart2pol(XX, YY)  # Cartesian coordinates

        # The list of Zernike polynomial functions
        list_zernike_polynomial = define_zernike()

        # In order to avoid layover in Fourier convolution we need to zero pad and then extract a part of image
        # computed from pupil_size and psf_size

        self.pad_pre = (
            ceil((self.pupil_size[0] - self.kernel_size[0]) / 2),
            ceil((self.pupil_size[1] - self.kernel_size[1]) / 2),
        )
        self.pad_post = (
            floor((self.pupil_size[0] - self.kernel_size[0]) / 2),
            floor((self.pupil_size[1] - self.kernel_size[1]) / 2),
        )

        # a list of indices of the parameters
        self.index_params = np.sort([int(param[1:]) for param in list_param])
        assert (
            np.max(self.index_params) <= 38
        ), "The Zernike polynomial index can not be exceed 38"

        # the number of Zernike coefficients
        self.n_zernike = len(self.index_params)

        # the tensor of Zernike polynomials in the pupil plane
        self.Z = torch.zeros(
            (self.pupil_size[0], self.pupil_size[1], self.n_zernike),
            **self.factory_kwargs,
        )
        for k in range(len(self.index_params)):
            self.Z[:, :, k] = list_zernike_polynomial[self.index_params[k]](
                XX, YY
            )  # defining the k-th Zernike polynomial

    def __update__(self):
        # self.factory_kwargs = {"device": self.params.device, "dtype": self.params.dtype}
        self.rho = self.rho.to(**self.factory_kwargs)
        self.Z = self.Z.to(**self.factory_kwargs)

    def step(self, batch_size: int = 1):
        r"""
        Generate a batch of PFS with a batch of Zernike coefficients

        :return: tensor B x psf_size x psf_size batch of psfs
        :rtype: torch.Tensor
        """
        self.__update__()

        ## add batch size to the shape. We can have a different batch size at each call of step()
        self.shape = (batch_size, self.shape[-3], self.shape[-2], self.shape[-1])

        coeff = self.generate_coeff()

        pupil1 = (self.Z @ coeff[:, : self.n_zernike].T).transpose(2, 0)
        pupil2 = torch.exp(-2.0j * torch.pi * pupil1)
        indicator = bump_function(self.rho, 1.0)
        pupil3 = pupil2 * indicator
        psf1 = torch.fft.ifftshift(torch.fft.fft2(torch.fft.fftshift(pupil3)))
        psf2 = torch.real(psf1 * torch.conj(psf1))

        psf3 = psf2[
            :,
            self.pad_pre[0] : self.pupil_size[0] - self.pad_post[0],
            self.pad_pre[1] : self.pupil_size[1] - self.pad_post[1],
        ].unsqueeze(1)
        psf = psf3 / torch.sum(psf3, dim=(-1, -2), keepdim=True)

        return {"filter": psf.expand(-1, self.shape[1], -1, -1)}

    def generate_coeff(self):
        batch_size = self.shape[0]
        coeff = torch.rand((batch_size, len(self.list_param)), **self.factory_kwargs)
        coeff = 2 * (coeff - 0.5) * self.max_zernike_amplitude
        return coeff


class ProductConvolutionBlurGenerator(PSFGenerator):
    r"""
    Generates a dictionary {'h', 'w'} of parameters to be used within :meth:`deepinv.physics.blur.SpaceVaryingBlur`

    :param tuple shape:
    :param list[str] list_param: list of activated Zernike coefficients, defaults to `["Z4", "Z5", "Z6","Z7", "Z8", "Z9", "Z10", "Z11"]`
    :param float fc: cutoff frequency (NA/emission_wavelength) * pixel_size. Should be in `[0, 1/4]` to respect Shannon, defaults to `0.2`
    :param tuple[int] pupil_size: this is used to synthesize the super-resolved pupil. The higher the more precise, defaults to (256, 256).
            If a int is given, a square pupil is considered.

    :return: a DiffractionBlurGenerator object

    |sep|

    :Examples:

    >>> generator = DiffractionBlurGenerator((16, 16))
    >>> filter = generator.step()
    >>> dinv.utils.plot(filter)
    >>> print(filter.shape)
    torch.Size([1, 1, 16, 16])

    """

    def __init__(
        self,
        shape: tuple,
        device: str = "cpu",
        dtype: type = torch.float32,
        l: float = 0.3,
        sigma: float = 0.25,
        n_steps: int = 1000,
    ) -> None:
        kwargs = {"l": l, "sigma": sigma, "n_steps": n_steps}
        super().__init__(shape=shape, device=device, dtype=dtype, **kwargs)

        n_psfs = 1024
        psf_size = 41
        generator = DiffractionBlurGenerator(
            (1, psf_size, psf_size), fc=0.25, device=device
        )
        psfs = generator.step(n_psfs)
        plot(psfs)

        # %%
        q = 10
        psfs_reshape = psfs.reshape(n_psfs, psf_size * psf_size)
        U, S, V = torch.svd_lowrank(psfs_reshape, q=q)
        eigen_psf = (V.T).reshape(q, psf_size, psf_size)[:, None, None]
        coeffs = psfs_reshape @ V
        mu = torch.mean(coeffs, 0)
        sigma = torch.std(coeffs, 0)

        plot(eigen_psf[:, 0])

        # %%
        spacing_psf = 2 * psf_size
        T0 = torch.linspace(0, 1, n0 // spacing_psf, device=device, dtype=dtype)
        T1 = torch.linspace(0, 1, n1 // spacing_psf, device=device, dtype=dtype)
        yy, xx = torch.meshgrid(T0, T1)
        X = torch.stack((yy.flatten(), xx.flatten()), dim=1)
        C = mu[None, :] + torch.randn(X.shape[0], q, device=device) * sigma[None, :]
        tps = ThinPlateSpline(0.0, device)
        tps.fit(X, C)
        T0 = torch.linspace(0, 1, n0, device=device, dtype=dtype)
        T1 = torch.linspace(0, 1, n1, device=device, dtype=dtype)
        yy, xx = torch.meshgrid(T0, T1)
        w = tps.transform(torch.stack((yy.flatten(), xx.flatten()), dim=1)).T
        w = w.reshape(q, n0, n1)[:, None, None]
        plot(w[:, 0])

        # %%
        params_blur = {"h": eigen_psf, "w": w, "padding": "reflect"}


class MotionBlurGenerator(PSFGenerator):
    r"""
    Random motion blurs generator, `reference <https://arxiv.org/pdf/1406.7444.pdf>`_.

    A blur trajectory is generated by sampling both its x- and y-coordinates independently
    from a Gaussian Process with a Matérn 3/2 covariance function.

    .. math::

        f_x(t), f_y(s) \sim \mathcal{GP}(0, k(t, s))

    where :math:`k(t,s)` is defined as

    .. math::

        k(t, s) = \sigma^2 \left( 1 + \frac{\sqrt{5} |t -s|}{l} + \frac{5 (t-s)^2}{3 l^2} \right) \exp \left(-\frac{\sqrt{5} |t-s|}{l}\right)

    :param float l: the length scale of the trajectory, defaults to 0.3
    :param float sigma: the standard deviation of the Gaussian Process, defaults to 0.25
    :param int n_steps: the number of points in the trajectory, defaults to 1000
    :param torch.device device: the device on which the kernel is generated, defaults to cpu
    :param torch.dtype dtype: the data type of the generated kernel, defaults to torch.float32

    |sep|

    :Examples:

    >>> generator = MotionBlurGenerator((1, 16, 16))
    >>> blur = generator.step()
    >>> blur = generator.step()
    >>> print(blur.shape)
    torch.Size([1, 1, 16, 16])


    :Examples:

    To generate new kernel, one can also call:

    >>> kernel = generator()
    >>> dinv.utils.plot(kernel)
    """

    def __init__(
        self,
        shape: tuple,
        device: str = "cpu",
        dtype: type = torch.float32,
        l: float = 0.3,
        sigma: float = 0.25,
        n_steps: int = 1000,
    ) -> None:
        kwargs = {"l": l, "sigma": sigma, "n_steps": n_steps}
        super().__init__(shape=shape, device=device, dtype=dtype, **kwargs)

    def matern_kernel(self, diff, sigma: float = None, l: float = None):
        if sigma is None:
            sigma = self.sigma
        if l is None:
            l = self.l
        fraction = 5**0.5 * diff.abs() / l
        return sigma**2 * (1 + fraction + fraction**2 / 3) * torch.exp(-fraction)

    # @torch.compile
    def f_matern(self, sigma: float = None, l: float = None):
        batch_size = self.shape[0]
        vec = torch.randn(batch_size, self.n_steps)
        time = torch.linspace(-torch.pi, torch.pi, self.n_steps)[None]

        kernel = self.matern_kernel(time, sigma, l)
        kernel_fft = torch.fft.rfft(kernel)
        vec_fft = torch.fft.rfft(vec)
        return torch.fft.irfft(vec_fft * torch.sqrt(kernel_fft)).real[
            :, torch.arange(self.n_steps // (2 * torch.pi)).type(torch.int)
        ]

    def step(self, batch_size: int = 1, sigma: float = None, l: float = None):
        r"""
        Generate a random motion blur PSF with parameters :math: '\sigma' and :math: `l`

        :param float sigma: the standard deviation of the Gaussian Process
        :param float l: the length scale of the trajectory

        :return: the generated PSF of shape `(batch_size, 1, kernel_size, kernel_size)`
        :rtype: torch.Tensor
        """
        ## add batch size to the shape. We can have a different batch size at each call of step()
        ## We enforce only one channel as the underlying generator code only works for one channel at the time
        if self.shape[0] != 1:
            self.shape = (batch_size, 1, self.shape[-2], self.shape[-1])
        else:
            self.shape = (batch_size, self.shape[-3], self.shape[-2], self.shape[-1])

        f_x = self.f_matern(sigma, l)[..., None]
        f_y = self.f_matern(sigma, l)[..., None]
        trajectories = torch.cat(
            (
                f_x - torch.mean(f_x, dim=1, keepdim=True),
                f_y - torch.mean(f_y, dim=1, keepdim=True),
            ),
            dim=-1,
        )
        kernels = [
            histogramdd(
                trajectory, bins=list(self.kernel_size), low=[-1, -1], upp=[1, 1]
            )[None, None].to(**self.factory_kwargs)
            for trajectory in trajectories
        ]
        kernel = torch.cat(kernels, dim=0)
        kernel = kernel / torch.sum(kernel, dim=(-2, -1), keepdim=True)

        return {"filter": kernel}


def define_zernike():
    r"""
    Returns a list of Zernike polynomials lambda functions in Cartesian coordinates

    :param list[func]: list of 37 lambda functions with the Zernike Polynomials. They are ordered as follows

        Z1:Z00 Piston or Bias
        Z2:Z11 x Tilt
        Z3:Z11 y Tilt
        Z4:Z20 Defocus
        Z5:Z22 Primary Astigmatism at 45
        Z6:Z22 Primary Astigmatism at 0
        Z7:Z31 Primary y Coma
        Z8:Z31 Primary x Coma
        Z9:Z33 y Trefoil
        Z10:Z33 x Trefoil
        Z11:Z40 Primary Spherical
        Z12:Z42 Secondary Astigmatism at 0
        Z13:Z42 Secondary Astigmatism at 45
        Z14:Z44 x Tetrafoil
        Z15:Z44 y Tetrafoil
        Z16:Z51 Secondary x Coma
        Z17:Z51 Secondary y Coma
        Z18:Z53 Secondary x Trefoil
        Z19:Z53 Secondary y Trefoil
        Z20:Z55 x Pentafoil
        Z21:Z55 y Pentafoil
        Z22:Z60 Secondary Spherical
        Z23:Z62 Tertiary Astigmatism at 45
        Z24:Z62 Tertiary Astigmatism at 0
        Z25:Z64 Secondary x Trefoil
        Z26:Z64 Secondary y Trefoil
        Z27:Z66 Hexafoil Y
        Z28:Z66 Hexafoil X
        Z29:Z71 Tertiary y Coma
        Z30:Z71 Tertiary x Coma
        Z31:Z73 Tertiary y Trefoil
        Z32:Z73 Tertiary x Trefoil
        Z33:Z75 Secondary Pentafoil Y
        Z34:Z75 Secondary Pentafoil X
        Z35:Z77 Heptafoil Y
        Z36:Z77 Heptafoil X
        Z37:Z80 Tertiary Spherical
    """
    Z = [None for k in range(38)]

    def r2(x, y):
        return x**2 + y**2

    sq3 = 3**0.5
    sq5 = 5**0.5
    sq6 = 6**0.5
    sq7 = 7**0.5
    sq8 = 8**0.5
    sq10 = 10**0.5
    sq12 = 12**0.5
    sq14 = 14**0.5

    Z[0] = lambda x, y: torch.ones_like(x)  # piston
    Z[1] = lambda x, y: torch.ones_like(x)  # piston
    Z[2] = lambda x, y: 2 * x  # tilt x
    Z[3] = lambda x, y: 2 * y  # tilt y
    Z[4] = lambda x, y: sq3 * (2 * r2(x, y) - 1)  # defocus
    Z[5] = lambda x, y: 2 * sq6 * x * y
    Z[6] = lambda x, y: sq6 * (x**2 - y**2)
    Z[7] = lambda x, y: sq8 * y * (3 * r2(x, y) - 2)
    Z[8] = lambda x, y: sq8 * x * (3 * r2(x, y) - 2)
    Z[9] = lambda x, y: sq8 * y * (3 * x**2 - y**2)
    Z[10] = lambda x, y: sq8 * x * (x**2 - 3 * y**2)
    Z[11] = lambda x, y: sq5 * (6 * r2(x, y) ** 2 - 6 * r2(x, y) + 1)
    Z[12] = lambda x, y: sq10 * (x**2 - y**2) * (4 * r2(x, y) - 3)
    Z[13] = lambda x, y: 2 * sq10 * x * y * (4 * r2(x, y) - 3)
    Z[14] = lambda x, y: sq10 * (r2(x, y) ** 2 - 8 * x**2 * y**2)
    Z[15] = lambda x, y: 4 * sq10 * x * y * (x**2 - y**2)
    Z[16] = lambda x, y: sq12 * x * (10 * r2(x, y) ** 2 - 12 * r2(x, y) + 3)
    Z[17] = lambda x, y: sq12 * y * (10 * r2(x, y) ** 2 - 12 * r2(x, y) + 3)
    Z[18] = lambda x, y: sq12 * x * (x**2 - 3 * y**2) * (5 * r2(x, y) - 4)
    Z[19] = lambda x, y: sq12 * y * (3 * x**2 - y**2) * (5 * r2(x, y) - 4)
    Z[20] = (
        lambda x, y: sq12
        * x
        * (16 * x**4 - 20 * x**2 * r2(x, y) + 5 * r2(x, y) ** 2)
    )
    Z[21] = (
        lambda x, y: sq12
        * y
        * (16 * y**4 - 20 * y**2 * r2(x, y) + 5 * r2(x, y) ** 2)
    )
    Z[22] = lambda x, y: sq7 * (
        20 * r2(x, y) ** 3 - 30 * r2(x, y) ** 2 + 12 * r2(x, y) - 1
    )
    Z[23] = lambda x, y: 2 * sq14 * x * y * (15 * r2(x, y) ** 2 - 20 * r2(x, y) + 6)
    Z[24] = (
        lambda x, y: sq14 * (x**2 - y**2) * (15 * r2(x, y) ** 2 - 20 * r2(x, y) + 6)
    )
    Z[25] = lambda x, y: 4 * sq14 * x * y * (x**2 - y**2) * (6 * r2(x, y) - 5)
    Z[26] = (
        lambda x, y: sq14
        * (8 * x**4 - 8 * x**2 * r2(x, y) + r2(x, y) ** 2)
        * (6 * r2(x, y) - 5)
    )
    Z[27] = (
        lambda x, y: sq14
        * x
        * y
        * (32 * x**4 - 32 * x**2 * r2(x, y) + 6 * r2(x, y) ** 2)
    )
    Z[28] = lambda x, y: sq14 * (
        32 * x**6
        - 48 * x**4 * r2(x, y)
        + 18 * x**2 * r2(x, y) ** 2
        - r2(x, y) ** 3
    )
    Z[29] = (
        lambda x, y: 4
        * y
        * (35 * r2(x, y) ** 3 - 60 * r2(x, y) ** 2 + 30 * r2(x, y) + 10)
    )
    Z[30] = (
        lambda x, y: 4
        * x
        * (35 * r2(x, y) ** 3 - 60 * r2(x, y) ** 2 + 30 * r2(x, y) + 10)
    )
    Z[31] = (
        lambda x, y: 4
        * y
        * (3 * x**2 - y**2)
        * (21 * r2(x, y) ** 2 - 30 * r2(x, y) + 10)
    )
    Z[32] = (
        lambda x, y: 4
        * x
        * (x**2 - 3 * y**2)
        * (21 * r2(x, y) ** 2 - 30 * r2(x, y) + 10)
    )
    Z[33] = (
        lambda x, y: 4
        * (7 * r2(x, y) - 6)
        * (
            4 * x**2 * y * (x**2 - y**2)
            + y * (r2(x, y) ** 2 - 8 * x**2 * y**2)
        )
    )
    Z[34] = lambda x, y: (
        4
        * (7 * r2(x, y) - 6)
        * (
            x * (r2(x, y) ** 2 - 8 * x**2 * y**2)
            - 4 * x * y**2 * (x**2 - y**2)
        )
    )
    Z[35] = lambda x, y: (
        8 * x**2 * y * (3 * r2(x, y) ** 2 - 16 * x**2 * y**2)
        + 4 * y * (x**2 - y**2) * (r2(x, y) ** 2 - 16 * x**2 * y**2)
    )
    Z[36] = lambda x, y: (
        4 * x * (x**2 - y**2) * (r2(x, y) ** 2 - 16 * x**2 * y**2)
        - 8 * x * y**2 * (3 * r2(x, y) ** 2 - 16 * x**2 * y**2)
    )
    Z[37] = lambda x, y: 3 * (
        70 * r2(x, y) ** 4
        - 140 * r2(x, y) ** 3
        + 90 * r2(x, y) ** 2
        - 20 * r2(x, y)
        + 1
    )
    return Z


def cart2pol(x, y):
    r"""
    Cartesian to polar coordinates

    :param torch.Tensor x: x coordinates
    :param torch.Tensor y: y coordinates

    :return: tuple (rho, phi) of torch.Tensor with radius and angle
    :rtype: tuple
    """

    rho = torch.sqrt(x**2 + y**2)
    # phi = torch.arctan2(y, x)
    return rho  # , phi


def bump_function(x, a=1.0, b=1.0):
    r"""
    Defines a function which is 1 on the interval [-a,a]
    and goes to 0 smoothly on [-a-b,-a]U[a,a+b] using a bump function
    For the discretization of indicator functions, we advise b=1, so that
    a=0, b=1 yields a bump.

    :param torch.Tensor x: tensor of arbitrary size
        input.
    :param Float a: radius (default is 1)
    :param Float b: interval on which the function goes to 0. (default is 1)

    :return: the bump function sampled at points x
    :rtype: torch.Tensor

    :Examples:

    >>> x = torch.linspace(-15, 15, 31)
    >>> X, Y = torch.meshgrid(x, x)
    >>> R = torch.sqrt(X**2 + Y**2)
    >>> Z = bump_function(R, 3, 1)
    >>> Z = Z / torch.sum(Z)
    >>> dinv.utils.plot(Z)
    """
    v = torch.zeros_like(x)
    v[torch.abs(x) <= a] = 1
    I = (torch.abs(x) > a) * (torch.abs(x) < a + b)
    v[I] = torch.exp(-1.0 / (1.0 - ((torch.abs(x[I]) - a) / b) ** 2)) / np.exp(-1.0)
    return v


# %%
if __name__ == "__main__":
    import deepinv as dinv

    generator = DiffractionBlurGenerator((16, 16))
    blur = generator.step()
    print(blur.shape)
    dinv.utils.plot(blur)

# %%
