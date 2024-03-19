from torch import Tensor
from typing import Tuple
from torchvision.transforms.functional import rotate
import torchvision
import torch.nn.functional as F
import torch
import numpy as np
import torch.fft as fft
from deepinv.physics.forward import Physics, LinearPhysics, DecomposablePhysics
from deepinv.utils import TensorList


def filter_fft(filter, img_size, real_fft=True):
    ph = int((filter.shape[2] - 1) / 2)
    pw = int((filter.shape[3] - 1) / 2)

    filt2 = torch.zeros(filter.shape[:2] + img_size[-2:], device=filter.device)

    filt2[:, : filter.shape[1], : filter.shape[2], : filter.shape[3]] = filter
    filt2 = torch.roll(filt2, shifts=(-ph, -pw), dims=(2, 3))

    return fft.rfft2(filt2) if real_fft else fft.fft2(filt2)


def gaussian_blur(sigma=(1, 1), angle=0):
    r"""
    Gaussian blur filter.

    :param float, tuple[float] sigma: standard deviation of the gaussian filter. If sigma is a float the filter is isotropic, whereas
        if sigma is a tuple of floats (sigma_x, sigma_y) the filter is anisotropic.
    :param float angle: rotation angle of the filter in degrees (only useful for anisotropic filters)
    """
    if isinstance(sigma, (int, float)):
        sigma = (sigma, sigma)

    s = max(sigma)
    c = int(s / 0.3 + 1)
    k_size = 2 * c + 1

    delta = torch.arange(k_size)

    x, y = torch.meshgrid(delta, delta, indexing="ij")
    x = x - c
    y = y - c
    filt = (x / sigma[0]).pow(2)
    filt += (y / sigma[1]).pow(2)
    filt = torch.exp(-filt / 2.0)

    filt = (
        rotate(
            filt.unsqueeze(0).unsqueeze(0),
            angle,
            interpolation=torchvision.transforms.InterpolationMode.BILINEAR,
        )
        .squeeze(0)
        .squeeze(0)
    )

    filt = filt / filt.flatten().sum()

    return filt.unsqueeze(0).unsqueeze(0)


def bilinear_filter(factor=2):
    x = np.arange(start=-factor + 0.5, stop=factor, step=1) / factor
    w = 1 - np.abs(x)
    w = np.outer(w, w)
    w = w / np.sum(w)
    return torch.Tensor(w).unsqueeze(0).unsqueeze(0)


def bicubic_filter(factor=2):
    x = np.arange(start=-2 * factor + 0.5, stop=2 * factor, step=1) / factor
    a = -0.5
    x = np.abs(x)
    w = ((a + 2) * np.power(x, 3) - (a + 3) * np.power(x, 2) + 1) * (x <= 1)
    w += (
        (a * np.power(x, 3) - 5 * a * np.power(x, 2) + 8 * a * x - 4 * a)
        * (x > 1)
        * (x < 2)
    )
    w = np.outer(w, w)
    w = w / np.sum(w)
    return torch.Tensor(w).unsqueeze(0).unsqueeze(0)


class Downsampling(LinearPhysics):
    r"""
    Downsampling operator for super-resolution problems.

    It is defined as

    .. math::

        y = S (h*x)

    where :math:`h` is a low-pass filter and :math:`S` is a subsampling operator.

    :param tuple[int] img_size: size of the input image
    :param int factor: downsampling factor
    :param torch.Tensor, str, NoneType filter: Downsampling filter. It can be ``'gaussian'``, ``'bilinear'`` or ``'bicubic'`` or a
        custom ``torch.Tensor`` filter. If ``None``, no filtering is applied.
    :param str padding: options are ``'valid'``, ``'circular'``, ``'replicate'`` and ``'reflect'``.
        If ``padding='valid'`` the blurred output is smaller than the image (no padding)
        otherwise the blurred output has the same size as the image.

    |sep|

    :Examples:

        Downsampling operator with a gaussian filter:

        >>> x = torch.zeros((1, 1, 32, 32)) # Define black image of size 32x32
        >>> x[:, :, 16, 16] = 1 # Define one white pixel in the middle
        >>> physics = Downsampling(img_size=((1, 1, 32, 32)), filter = "gaussian", factor = 2)
        >>> y = physics(x)
        >>> y[:, :, 7:10, 7:10] # Display the center of the downsampled image
        tensor([[[[0.0146, 0.0241, 0.0146],
                  [0.0241, 0.0398, 0.0241],
                  [0.0146, 0.0241, 0.0146]]]])

    """

    def __init__(
        self,
        img_size,
        params,
        factor=2,
        device="cpu",
        padding="circular",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.factor = factor
        assert isinstance(
            factor, int), "downsampling factor should be an integer"
        self.imsize = img_size
        self.padding = padding
        if isinstance(params, torch.nn.Parameter):
            self.params = params.requires_grad_(False).to(device)
        if isinstance(params, torch.Tensor):
            self.params = torch.nn.Parameter(self.params, requires_grad=False).to(device)    
        elif params is None:
            self.params = params
        elif params == "gaussian":
            self.params = torch.nn.Parameter(
                gaussian_blur(sigma=(factor, factor)),
                              requires_grad=False
                              ).to(device)
        elif params == "bilinear":
            self.params = torch.nn.Parameter(
                bilinear_filter(
                self.factor), requires_grad=False).to(device)
        elif params == "bicubic":
            self.params = torch.nn.Parameter(
                bicubic_filter(
                self.factor), requires_grad=False).to(device)
        else:
            raise Exception("The chosen downsampling filter doesn't exist")

        if self.params is not None:
            self.Fh = filter_fft(self.params, img_size,
                                 real_fft=False).to(device)
            self.Fhc = torch.conj(self.Fh)
            self.Fh2 = self.Fhc * self.Fh
            #self.params = torch.nn.Parameter(self.params, requires_grad=False)
            self.Fhc = torch.nn.Parameter(self.Fhc, requires_grad=False)
            self.Fh2 = torch.nn.Parameter(self.Fh2, requires_grad=False)

    def A(self, x):
        if self.params is not None:
            x = conv(x, self.params, padding=self.padding)
        x = x[:, :, :: self.factor, :: self.factor]  # downsample
        return x

    def A_adjoint(self, y):
        x = torch.zeros((y.shape[0],) + self.imsize, device=y.device)
        x[:, :, :: self.factor, :: self.factor] = y  # upsample
        if self.params is not None:
            x = conv_transpose(x, self.params, padding=self.padding)
        return x

    def prox_l2(self, z, y, gamma, use_fft=True):
        r"""
        If the padding is circular, it computes the proximal operator with the closed-formula of
        https://arxiv.org/abs/1510.00143.

        Otherwise, it computes it using the conjugate gradient algorithm which can be slow if applied many times.
        """

        if use_fft and self.padding == "circular":  # Formula from (Zhao, 2016)
            z_hat = self.A_adjoint(y) + 1 / gamma * z
            Fz_hat = fft.fft2(z_hat)

            def splits(a, sf):
                """split a into sfxsf distinct blocks
                Args:
                    a: NxCxWxH
                    sf: split factor
                Returns:
                    b: NxCx(W/sf)x(H/sf)x(sf^2)
                """
                b = torch.stack(torch.chunk(a, sf, dim=2), dim=4)
                b = torch.cat(torch.chunk(b, sf, dim=3), dim=4)
                return b

            top = torch.mean(splits(self.Fh * Fz_hat, self.factor), dim=-1)
            below = torch.mean(
                splits(self.Fh2, self.factor), dim=-1) + 1 / gamma
            rc = self.Fhc * (top / below).repeat(1, 1,
                                                 self.factor, self.factor)
            r = torch.real(fft.ifft2(rc))
            return (z_hat - r) * gamma
        else:
            return LinearPhysics.prox_l2(self, z, y, gamma)


def extend_filter(filter):
    b, c, h, w = filter.shape
    w_new = w
    h_new = h

    offset_w = 0
    offset_h = 0

    if w == 1:
        w_new = 3
        offset_w = 1
    elif w % 2 == 0:
        w_new += 1

    if h == 1:
        h_new = 3
        offset_h = 1
    elif h % 2 == 0:
        h_new += 1

    out = torch.zeros((b, c, h_new, w_new), device=filter.device)
    out[:, :, offset_h: h + offset_h, offset_w: w + offset_w] = filter
    return out


def conv(x: Tensor, filter: Tensor, padding: str = 'valid'):
    r"""
    Convolution of x and filter. The transposed of this operation is conv_transpose(x, filter, padding)

    :param x: (torch.Tensor) Image of size `(B, C, W, H)`.
    :param filter: (torch.Tensor) Filter of size `(b, c, w, h)` ) where `b` can be either `1` or `B` and `c` can be either `1` or `C`.

    :param padding: ( options = 'valid', 'circular', 'replicate', 'reflect'. If padding='valid' the blurred output is smaller than the image (no padding), otherwise the blurred output has the same size as the image.

    """
    assert x.dim() == filter.dim() == 4, 'Input and filter must be 4D tensors'

    # Get dimensions of the input and the filter
    B, C, H, W = x.size()
    b, c, h, w = filter.size()

    if c != C:
        assert c == 1
        print('Warning: the number of channels of the input is different than the one of the filter. The filter is expanded in the channel dimension')
        filter = filter.expand(-1, C, -1, -1)

    if b != B:
        assert b == 1
        filter = filter.expand(B, -1, -1, -1)

    if padding != "valid":
        ph = int((h - 1) / 2)
        pw = int((w - 1) / 2)
        x = F.pad(x, (pw, pw, ph, ph), mode=padding, value=0)
        B, C, H, W = x.size()

    # Move batch dim of the input into channels
    x = x.reshape(1, -1, H, W)
    # Expand the channel dim of the filter and move it into batch dimension
    filter = filter.reshape(B * C, -1, h, w)
    # Perform the convolution, using the groups parameter
    output = F.conv2d(x, filter, padding='valid', groups=B * C)
    # Make it in the good shape
    output = output.view(B, C, output.size(-2), -1)

    return output


def conv_transpose(y: Tensor, filter: Tensor, padding: str = 'valid'):
    r"""
    Transposed convolution of x and filter. The transposed of this operation is conv(x, filter, padding)

    :param torch.Tensor x: Image of size `(B, C, W, H)`.
    :param torch.Tensor filter: Filter of size `(b, c, w, h)` ) where `b` can be either `1` or `B` and `c` can be either `1` or `C`.
    :param str padding: options are ``'valid'``, ``'circular'``, ``'replicate'`` and ``'reflect'``.
        If ``padding='valid'`` the blurred output is smaller than the image (no padding)
        otherwise the blurred output has the same size as the image.
    """

    assert y.dim() == filter.dim() == 4, 'Input and filter must be 4D tensors'

    # Get dimensions of the input and the filter
    B, C, H, W = y.size()
    b, c, h, w = filter.size()

    ph = int((h - 1) / 2)
    pw = int((w - 1) / 2)

    if c != C:
        assert c == 1
        filter = filter.expand(-1, C, -1, -1)

    if b != B:
        assert b == 1
        filter = filter.expand(B, -1, -1, -1)

    # Move batch dim of the input into channels
    y = y.reshape(1, -1, H, W)
    # Expand the channel dim of the filter and move it into batch dimension
    filter = filter.reshape(B * C, -1, h, w)
    # Perform the convolution, using the groups parameter
    x = F.conv_transpose2d(y, filter, groups=B * C)
    # Make it in the good shape
    x = x.view(B, C, x.size(-2), -1)

    if padding == "valid":
        out = x
    elif padding == "zero":
        out = x[:, :, ph:-ph, pw:-pw]
    elif padding == "circular":
        out = x[:, :, ph:-ph, pw:-pw]
        # sides
        out[:, :, :ph, :] += x[:, :, -ph:, pw:-pw]
        out[:, :, -ph:, :] += x[:, :, :ph, pw:-pw]
        out[:, :, :, :pw] += x[:, :, ph:-ph, -pw:]
        out[:, :, :, -pw:] += x[:, :, ph:-ph, :pw]
        # corners
        out[:, :, :ph, :pw] += x[:, :, -ph:, -pw:]
        out[:, :, -ph:, -pw:] += x[:, :, :ph, :pw]
        out[:, :, :ph, -pw:] += x[:, :, -ph:, :pw]
        out[:, :, -ph:, :pw] += x[:, :, :ph, -pw:]

    elif padding == "reflect":
        out = x[:, :, ph:-ph, pw:-pw]
        # sides
        out[:, :, 1: 1 + ph, :] += x[:, :, :ph, pw:-pw].flip(dims=(2,))
        out[:, :, -ph - 1: -1, :] += x[:, :, -ph:, pw:-pw].flip(dims=(2,))
        out[:, :, :, 1: 1 + pw] += x[:, :, ph:-ph, :pw].flip(dims=(3,))
        out[:, :, :, -pw - 1: -1] += x[:, :, ph:-ph, -pw:].flip(dims=(3,))
        # corners
        out[:, :, 1: 1 + ph, 1: 1 + pw] += x[:, :, :ph, :pw].flip(dims=(2, 3))
        out[:, :, -ph - 1: -1, -pw - 1: -1] += x[:,
                                                 :, -ph:, -pw:].flip(dims=(2, 3))
        out[:, :, -ph - 1: -1, 1: 1 + pw] += x[:,
                                               :, -ph:, :pw].flip(dims=(2, 3))
        out[:, :, 1: 1 + ph, -pw - 1: -1] += x[:,
                                               :, :ph, -pw:].flip(dims=(2, 3))

    elif padding == "replicate":
        out = x[:, :, ph:-ph, pw:-pw]
        # sides
        out[:, :, 0, :] += x[:, :, :ph, pw:-pw].sum(2)
        out[:, :, -1, :] += x[:, :, -ph:, pw:-pw].sum(2)
        out[:, :, :, 0] += x[:, :, ph:-ph, :pw].sum(3)
        out[:, :, :, -1] += x[:, :, ph:-ph, -pw:].sum(3)
        # corners
        out[:, :, 0, 0] += x[:, :, :ph, :pw].sum(3).sum(2)
        out[:, :, -1, -1] += x[:, :, -ph:, -pw:].sum(3).sum(2)
        out[:, :, -1, 0] += x[:, :, -ph:, :pw].sum(3).sum(2)
        out[:, :, 0, -1] += x[:, :, :ph, -pw:].sum(3).sum(2)

    return out


class BlindBlur(Physics):
    r"""
    Blind blur operator.

    If performs

    .. math::

        y = w*x

    where :math:`*` denotes convolution and :math:`w` is an unknown filter.
    This class uses ``torch.conv2d`` for performing the convolutions.

    The signal is described by a tuple (x,w) where the first element is the clean image, and the second element
    is the blurring kernel. The measurements y are a tensor representing the convolution of x and w.

    :param int kernel_size: maximum support size of the (unknown) blurring kernels.
    :param str padding: options are ``'valid'``, ``'circular'``, ``'replicate'`` and ``'reflect'``.
        If ``padding='valid'`` the blurred output is smaller than the image (no padding)
        otherwise the blurred output has the same size as the image.

    """

    def __init__(self, kernel_size=3, padding="circular", **kwargs):
        super().__init__(**kwargs)
        self.padding = padding

        if type(kernel_size) is not list or type(kernel_size) is not tuple:
            self.kernel_size = [kernel_size, kernel_size]

    def A(self, s):
        r"""

        :param tuple, list, deepinv.utils.ListTensor x: List containing two torch.tensor, x[0] with the image and x[1] with the filter.
        :return: (torch.tensor) blurred measurement.
        """
        x = s[0]
        w = s[1]
        return conv(x, w, self.padding)

    def A_dagger(self, y):
        r"""
        Returns the trivial inverse where x[0] = blurry input and x[1] with a delta filter, such that
        the convolution of x[0] and x[1] is y.

        .. note:

            This trivial inverse can be useful for some reconstruction networks, such as ``deepinv.models.ArtifactRemoval``.

        :param torch.Tensor y: blurred measurement.
        :return: Tuple containing the trivial inverse.
        """
        x = y.clone()
        mid_h = int(self.kernel_size[0] / 2)
        mid_w = int(self.kernel_size[1] / 2)
        w = torch.zeros(
            (y.shape[0], 1, self.kernel_size[0], self.kernel_size[1]))
        w[:, :, mid_h, mid_w] = 1.0

        return TensorList([x, w])


class Blur(LinearPhysics):
    r"""

    Blur operator.

    This forward operator performs

    .. math:: y = w*x

    where :math:`*` denotes convolution and :math:`w` is a filter.

    This class uses :meth:`torch.nn.functional.conv2d` for performing the convolutions.

    :param torch.Tensor filter: Tensor of size (1, 1, H, W) or (1, C, H, W) containing the blur filter, e.g., :meth:`deepinv.physics.blur.gaussian_blur`.
    :param str padding: options are ``'valid'``, ``'circular'``, ``'replicate'`` and ``'reflect'``. If ``padding='valid'`` the blurred output is smaller than the image (no padding)
        otherwise the blurred output has the same size as the image.
    :param str device: cpu or cuda.

    |sep|

    :Examples:

        Blur operator with a basic averaging filter applied to a 16x16 black image with
        a single white pixel in the center:

        >>> x = torch.zeros((1, 1, 16, 16)) # Define black image of size 16x16
        >>> x[:, :, 8, 8] = 1 # Define one white pixel in the middle
        >>> w = torch.ones((1, 1, 2, 2)) / 4 # Basic 2x2 averaging filter
        >>> physics = Blur(filter=w)
        >>> y = physics(x)
        >>> y[:, :, 7:10, 7:10] # Display the center of the blurred image
        tensor([[[[0.0000, 0.0000, 0.0000],
                  [0.0000, 0.2500, 0.2500],
                  [0.0000, 0.2500, 0.2500]]]])

    """

    def __init__(self, params, padding="circular", device="cpu", **kwargs):
        super().__init__(**kwargs)
        self.padding = padding
        self.device = device
        if isinstance(params, torch.nn.Parameter):
            self.params = params.requires_grad_(False).to(device)
        if isinstance(params, torch.Tensor):
            self.params = torch.nn.Parameter(self.params, requires_grad=False).to(device)    

    def A(self, x):
        return conv(x, self.filter, self.padding)

    def A_adjoint(self, y):
        return conv_transpose(y, self.filter, self.padding)


class BlurFFT(DecomposablePhysics):
    """

    FFT-based blur operator.

    It performs the operation

    .. math:: y = w*x

    where :math:`*` denotes convolution and :math:`w` is a filter.

    Blur operator based on ``torch.fft`` operations, which assumes a circular padding of the input, and allows for
    the singular value decomposition via ``deepinv.Physics.DecomposablePhysics`` and has fast pseudo-inverse and prox operators.



    :param tuple img_size: Input image size in the form (C, H, W).
    :param torch.Tensor filter: torch.Tensor of size (1, 1, H, W) or (1, C, H, W) containing the blur filter, e.g.,
        :meth:`deepinv.physics.blur.gaussian_blur`.
    :param str device: cpu or cuda

    |sep|

    :Examples:

        BlurFFT operator with a basic averaging filter applied to a 16x16 black image with
        a single white pixel in the center:

        >>> x = torch.zeros((1, 1, 16, 16)) # Define black image of size 16x16
        >>> x[:, :, 8, 8] = 1 # Define one white pixel in the middle
        >>> filter = torch.ones((1, 1, 2, 2)) / 4 # Basic 2x2 filter
        >>> physics = BlurFFT(img_size=(1, 1, 16, 16), filter=filter)
        >>> y = physics(x)
        >>> y[:, :, 7:10, 7:10] # Display the center of the blurred image
        tensor([[[[ 2.5000e-01,  2.5000e-01, -3.1177e-10],
                  [ 2.5000e-01,  2.5000e-01, -7.1280e-10],
                  [-7.5937e-10, -5.4986e-10,  3.9221e-10]]]])

    """

    def __init__(self, img_size, params, device="cpu", **kwargs):
        super().__init__(**kwargs)
        self.img_size = img_size

        if isinstance(params, torch.nn.Parameter):
            self.params = params.requires_grad_(False).to(device)
        if isinstance(params, torch.Tensor):
            self.params = torch.nn.Parameter(params, requires_grad=False).to(device)    

        if img_size[0] > params.shape[1]:
            params = params.repeat(1, img_size[0], 1, 1)

        self.params = filter_fft(params, img_size).to("cpu")
        self.angle = torch.angle(self.params)
        self.angle = torch.exp(-1j * self.angle).to(device)
        self.params = torch.abs(self.params).unsqueeze(-1)
        self.params = torch.cat([self.params, self.params], dim=-1)

        self.params = torch.nn.Parameter(
            self.params, requires_grad=False).to(device)

    def V_adjoint(self, x):
        return torch.view_as_real(
            fft.rfft2(x, norm="ortho")
        )  # make it a true SVD (see J. Romberg notes)

    def U(self, x):
        return fft.irfft2(
            torch.view_as_complex(x) * self.angle, norm="ortho", s=self.img_size[-2:]
        )

    def U_adjoint(self, x):
        return torch.view_as_real(
            fft.rfft2(x, norm="ortho") * torch.conj(self.angle)
        )  # make it a true SVD (see J. Romberg notes)

    def V(self, x):
        return fft.irfft2(torch.view_as_complex(x), norm="ortho", s=self.img_size[-2:])


# # test code
# if __name__ == "__main__":
#     device = "cuda:0"
#
#     import matplotlib.pyplot as plt
#
#     device = "cuda:0"
#     x = torchvision.io.read_image("../../datasets/celeba/img_align_celeba/085307.jpg")
#     x = x.unsqueeze(0).float().to(device) / 255
#     x = torchvision.transforms.Resize((160, 180))(x)
#
#     sigma_noise = 0.0
#     kernel = torch.zeros((1, 1, 15, 15), device=device)
#     kernel[:, :, 7, :] = 1 / 15
#     physics = Downsampling(img_size=x.shape[1:], filter="bilinear", device=device)
#     physics2 = Blur(img_size=x.shape[1:], filter=kernel, device=device)
#
#     y = physics(x)
#     y2 = physics2(x)
#
#     xhat = physics.V(physics.U_adjoint(y) / physics.mask)
#     xhat2 = physics2.A_dagger(y2)
#
#     print(xhat.shape)
#     # print(physics.adjointness_test(x))
#     print(torch.sum((y - y2).pow(2)))
#     print(torch.sum((xhat - xhat2).pow(2)))
#
#     print(torch.sum((x - xhat).pow(2)))
#     print(torch.sum((x - xhat2).pow(2)))
#
#     print(physics.compute_norm(x))
#     print(physics.adjointness_test(x))
#     xhat = physics.prox_l2(y, y, gamma=1.0)
#
#     xhat = physics.A_dagger(y)
#
#     plt.imshow(x.squeeze(0).permute(1, 2, 0).cpu().numpy())
#     plt.show()
#     plt.imshow(y.squeeze(0).permute(1, 2, 0).cpu().numpy())
#     plt.show()
#     plt.imshow(xhat.squeeze(0).permute(1, 2, 0).cpu().numpy())
#     plt.show()
#     plt.imshow(xhat2.squeeze(0).permute(1, 2, 0).cpu().numpy())
#     plt.show()
#
#     plt.imshow(physics.A(xhat).squeeze(0).permute(1, 2, 0).cpu().numpy())
#     plt.show()
