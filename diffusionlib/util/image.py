"""Utility functions for working with images."""

import matplotlib.pyplot as plt
import numpy as np
import scipy
import torch
import torch.nn.functional as F
from fastmri.fftc import fft2c_new, ifft2c_new
from PIL import Image
from torch import nn
from torch.autograd import Variable

from external.motionblur.motionblur import Kernel
from external.resizer import resizer


class Resizer(nn.Module):
    def __init__(
        self, in_shape, scale_factor=None, output_shape=None, kernel=None, antialiasing=True
    ):
        super().__init__()

        # First standardize values and fill missing arguments (if needed) by deriving scale from output shape or vice versa
        scale_factor, output_shape = self.fix_scale_and_size(in_shape, output_shape, scale_factor)

        # Choose interpolation method, each method has the matching kernel size
        method, kernel_width = {
            "cubic": (resizer.cubic, 4.0),
            "lanczos2": (resizer.lanczos2, 4.0),
            "lanczos3": (resizer.lanczos3, 6.0),
            "box": (resizer.box, 1.0),
            "linear": (resizer.linear, 2.0),
            None: (resizer.cubic, 4.0),  # set default interpolation method as cubic
        }.get(kernel)

        # Antialiasing is only used when downscaling
        antialiasing *= np.any(np.array(scale_factor) < 1)

        # Sort indices of dimensions according to scale of each dimension. since we are going dim by dim this is efficient
        sorted_dims = np.argsort(np.array(scale_factor))
        self.sorted_dims = [int(dim) for dim in sorted_dims if scale_factor[dim] != 1]

        # Iterate over dimensions to calculate local weights for resizing and resize each time in one direction
        field_of_view_list = []
        weights_list = []
        for dim in self.sorted_dims:
            # for each coordinate (along 1 dim), calculate which coordinates in the input image affect its result and the
            # weights that multiply the values there to get its result.
            weights, field_of_view = self.contributions(
                in_shape[dim],
                output_shape[dim],
                scale_factor[dim],
                method,
                kernel_width,
                antialiasing,
            )

            # convert to torch tensor
            weights = torch.tensor(weights.T, dtype=torch.float32)

            # We add singleton dimensions to the weight matrix so we can multiply it with the big tensor we get for
            # tmp_im[field_of_view.T], (bsxfun style)
            weights_list.append(
                nn.Parameter(
                    torch.reshape(weights, list(weights.shape) + (len(scale_factor) - 1) * [1]),
                    requires_grad=False,
                )
            )
            field_of_view_list.append(
                nn.Parameter(
                    torch.tensor(field_of_view.T.astype(np.int32), dtype=torch.long),
                    requires_grad=False,
                )
            )

        self.field_of_view = nn.ParameterList(field_of_view_list)
        self.weights = nn.ParameterList(weights_list)

    def forward(self, in_tensor):
        x = in_tensor

        # Use the affecting position values and the set of weights to calculate the result of resizing along this 1 dim
        for dim, fov, w in zip(self.sorted_dims, self.field_of_view, self.weights):
            # To be able to act on each dim, we swap so that dim 0 is the wanted dim to resize
            x = torch.transpose(x, dim, 0)

            # This is a bit of a complicated multiplication: x[field_of_view.T] is a tensor of order image_dims+1.
            # for each pixel in the output-image it matches the positions the influence it from the input image (along 1 dim
            # only, this is why it only adds 1 dim to 5the shape). We then multiply, for each pixel, its set of positions with
            # the matching set of weights. we do this by this big tensor element-wise multiplication (MATLAB bsxfun style:
            # matching dims are multiplied element-wise while singletons mean that the matching dim is all multiplied by the
            # same number
            x = torch.sum(x[fov] * w, dim=0)

            # Finally we swap back the axes to the original order
            x = torch.transpose(x, dim, 0)

        return x

    def fix_scale_and_size(self, input_shape, output_shape, scale_factor):
        # First fixing the scale-factor (if given) to be standardized the function expects (a list of scale factors in the
        # same size as the number of input dimensions)
        if scale_factor is not None:
            # By default, if scale-factor is a scalar we assume 2d resizing and duplicate it.
            if np.isscalar(scale_factor) and len(input_shape) > 1:
                scale_factor = [scale_factor, scale_factor]

            # We extend the size of scale-factor list to the size of the input by assigning 1 to all the unspecified scales
            scale_factor = list(scale_factor)
            scale_factor = [1] * (len(input_shape) - len(scale_factor)) + scale_factor

        # Fixing output-shape (if given): extending it to the size of the input-shape, by assigning the original input-size
        # to all the unspecified dimensions
        if output_shape is not None:
            output_shape = list(input_shape[len(output_shape) :]) + list(
                np.uint(np.array(output_shape))
            )

        # Dealing with the case of non-give scale-factor, calculating according to output-shape. note that this is
        # sub-optimal, because there can be different scales to the same output-shape.
        if scale_factor is None:
            scale_factor = 1.0 * np.array(output_shape) / np.array(input_shape)

        # Dealing with missing output-shape. calculating according to scale-factor
        if output_shape is None:
            output_shape = np.uint(np.ceil(np.array(input_shape) * np.array(scale_factor)))

        return scale_factor, output_shape

    def contributions(self, in_length, out_length, scale, kernel, kernel_width, antialiasing):
        # This function calculates a set of 'filters' and a set of field_of_view that will later on be applied
        # such that each position from the field_of_view will be multiplied with a matching filter from the
        # 'weights' based on the interpolation method and the distance of the sub-pixel location from the pixel centers
        # around it. This is only done for one dimension of the image.

        # When anti-aliasing is activated (default and only for downscaling) the receptive field is stretched to size of
        # 1/sf. this means filtering is more 'low-pass filter'.
        fixed_kernel = (lambda arg: scale * kernel(scale * arg)) if antialiasing else kernel
        kernel_width *= 1.0 / scale if antialiasing else 1.0

        # These are the coordinates of the output image
        out_coordinates = np.arange(1, out_length + 1)

        # since both scale-factor and output size can be provided simulatneously, perserving the center of the image requires shifting
        # the output coordinates. the deviation is because out_length doesn't necesary equal in_length*scale.
        # to keep the center we need to subtract half of this deivation so that we get equal margins for boths sides and center is preserved.
        shifted_out_coordinates = out_coordinates - (out_length - in_length * scale) / 2

        # These are the matching positions of the output-coordinates on the input image coordinates.
        # Best explained by example: say we have 4 horizontal pixels for HR and we downscale by SF=2 and get 2 pixels:
        # [1,2,3,4] -> [1,2]. Remember each pixel number is the middle of the pixel.
        # The scaling is done between the distances and not pixel numbers (the right boundary of pixel 4 is transformed to
        # the right boundary of pixel 2. pixel 1 in the small image matches the boundary between pixels 1 and 2 in the big
        # one and not to pixel 2. This means the position is not just multiplication of the old pos by scale-factor).
        # So if we measure distance from the left border, middle of pixel 1 is at distance d=0.5, border between 1 and 2 is
        # at d=1, and so on (d = p - 0.5).  we calculate (d_new = d_old / sf) which means:
        # (p_new-0.5 = (p_old-0.5) / sf)     ->          p_new = p_old/sf + 0.5 * (1-1/sf)
        match_coordinates = shifted_out_coordinates / scale + 0.5 * (1 - 1 / scale)

        # This is the left boundary to start multiplying the filter from, it depends on the size of the filter
        left_boundary = np.floor(match_coordinates - kernel_width / 2)

        # Kernel width needs to be enlarged because when covering has sub-pixel borders, it must 'see' the pixel centers
        # of the pixels it only covered a part from. So we add one pixel at each side to consider (weights can zeroize them)
        expanded_kernel_width = np.ceil(kernel_width) + 2

        # Determine a set of field_of_view for each each output position, these are the pixels in the input image
        # that the pixel in the output image 'sees'. We get a matrix whos horizontal dim is the output pixels (big) and the
        # vertical dim is the pixels it 'sees' (kernel_size + 2)
        field_of_view = np.squeeze(
            np.int16(np.expand_dims(left_boundary, axis=1) + np.arange(expanded_kernel_width) - 1)
        )

        # Assign weight to each pixel in the field of view. A matrix whos horizontal dim is the output pixels and the
        # vertical dim is a list of weights matching to the pixel in the field of view (that are specified in
        # 'field_of_view')
        weights = fixed_kernel(1.0 * np.expand_dims(match_coordinates, axis=1) - field_of_view - 1)

        # Normalize weights to sum up to 1. be careful from dividing by 0
        sum_weights = np.sum(weights, axis=1)
        sum_weights[sum_weights == 0] = 1.0
        weights = 1.0 * weights / np.expand_dims(sum_weights, axis=1)

        # We use this mirror structure as a trick for reflection padding at the boundaries
        mirror = np.uint(
            np.concatenate((np.arange(in_length), np.arange(in_length - 1, -1, step=-1)))
        )
        field_of_view = mirror[np.mod(field_of_view, mirror.shape[0])]

        # Get rid of  weights and pixel positions that are of zero weight
        non_zero_out_pixels = np.nonzero(np.any(weights, axis=0))
        weights = np.squeeze(weights[:, non_zero_out_pixels])
        field_of_view = np.squeeze(field_of_view[:, non_zero_out_pixels])

        # Final products are the relative positions and the matching weights, both are output_size X fixed_kernel_size
        return weights, field_of_view


class Blurkernel(nn.Module):
    def __init__(self, blur_type="gaussian", kernel_size=31, std=3.0, device=None):
        super().__init__()
        self.blur_type = blur_type
        self.kernel_size = kernel_size
        self.std = std
        self.device = device
        self.seq = nn.Sequential(
            nn.ReflectionPad2d(self.kernel_size // 2),
            nn.Conv2d(3, 3, self.kernel_size, stride=1, padding=0, bias=False, groups=3),
        )

        self.weights_init()

    def forward(self, x):
        return self.seq(x)

    def weights_init(self):
        if self.blur_type == "gaussian":
            n = np.zeros((self.kernel_size, self.kernel_size))
            n[self.kernel_size // 2, self.kernel_size // 2] = 1
            k = scipy.ndimage.gaussian_filter(n, sigma=self.std)
            k = torch.from_numpy(k)
            self.k = k
            for name, f in self.named_parameters():
                f.data.copy_(k)
        elif self.blur_type == "motion":
            k = Kernel(size=(self.kernel_size, self.kernel_size), intensity=self.std).kernelMatrix
            k = torch.from_numpy(k)
            self.k = k
            for name, f in self.named_parameters():
                f.data.copy_(k)

    def update_weights(self, k):
        if not torch.is_tensor(k):
            k = torch.from_numpy(k).to(self.device)
        for name, f in self.named_parameters():
            f.data.copy_(k)

    def get_kernel(self):
        return self.k


class Unfolder:
    def __init__(self, img_size=256, crop_size=128, stride=64):
        self.img_size = img_size
        self.crop_size = crop_size
        self.stride = stride

        self.unfold = nn.Unfold(crop_size, stride=stride)
        self.dim_size = (img_size - crop_size) // stride + 1

    def __call__(self, x):
        patch1D = self.unfold(x)
        patch2D = reshape_patch(patch1D, crop_size=self.crop_size, dim_size=self.dim_size)
        return patch2D


class Folder:
    def __init__(self, img_size=256, crop_size=128, stride=64):
        self.img_size = img_size
        self.crop_size = crop_size
        self.stride = stride

        self.fold = nn.Fold(img_size, crop_size, stride=stride)
        self.dim_size = (img_size - crop_size) // stride + 1

    def __call__(self, patch2D):
        patch1D = reshape_patch_back(patch2D, crop_size=self.crop_size, dim_size=self.dim_size)
        return self.fold(patch1D)


class mask_generator:
    def __init__(
        self, mask_type, mask_len_range=None, mask_prob_range=None, image_size=256, margin=(16, 16)
    ):
        """
        (mask_len_range): given in (min, max) tuple.
        Specifies the range of box size in each dimension
        (mask_prob_range): for the case of random masking,
        specify the probability of individual pixels being masked
        """
        assert mask_type in ["box", "random", "both", "extreme"]
        self.mask_type = mask_type
        self.mask_len_range = mask_len_range
        self.mask_prob_range = mask_prob_range
        self.image_size = image_size
        self.margin = margin

    def __call__(self, img):
        if self.mask_type == "random":
            mask = self._retrieve_random(img)
            return mask
        elif self.mask_type == "box":
            mask, t, th, w, wl = self._retrieve_box(img)
            return mask
        elif self.mask_type == "extreme":
            mask, t, th, w, wl = self._retrieve_box(img)
            mask = 1.0 - mask
            return mask

    def _retrieve_box(self, img):
        l, h = self.mask_len_range
        l, h = int(l), int(h)
        mask_h = np.random.randint(l, h)
        mask_w = np.random.randint(l, h)
        mask, t, tl, w, wh = random_sq_bbox(
            img, mask_shape=(mask_h, mask_w), image_size=self.image_size, margin=self.margin
        )
        return mask, t, tl, w, wh

    def _retrieve_random(self, img):
        total = self.image_size**2
        # random pixel sampling
        l, h = self.mask_prob_range
        prob = np.random.uniform(l, h)
        mask_vec = torch.ones([1, self.image_size * self.image_size])
        samples = np.random.choice(
            self.image_size * self.image_size, int(total * prob), replace=False
        )
        mask_vec[:, samples] = 0
        mask_b = mask_vec.view(1, self.image_size, self.image_size)
        mask_b = mask_b.repeat(3, 1, 1)
        mask = torch.ones_like(img, device=img.device)
        mask[:, ...] = mask_b
        return mask


def fft2(x):
    """FFT with shifting DC to the center of the image"""
    return torch.fft.fftshift(torch.fft.fft2(x), dim=[-1, -2])


def ifft2(x):
    """IFFT with shifting DC to the corner of the image prior to transform"""
    return torch.fft.ifft2(torch.fft.ifftshift(x, dim=[-1, -2]))


def fft2_m(x):
    """FFT for multi-coil"""
    if not torch.is_complex(x):
        x = x.type(torch.complex64)
    return torch.view_as_complex(fft2c_new(torch.view_as_real(x)))


def ifft2_m(x):
    """IFFT for multi-coil"""
    if not torch.is_complex(x):
        x = x.type(torch.complex64)
    return torch.view_as_complex(ifft2c_new(torch.view_as_real(x)))


def clear(x):
    x = x.detach().cpu().squeeze().numpy()
    return normalize_np(x)


def clear_color(x):
    if torch.is_complex(x):
        x = torch.abs(x)
    x = x.detach().cpu().squeeze().numpy()
    return normalize_np(np.transpose(x, (1, 2, 0)))


def normalize_np(img):
    """Normalize img in arbitrary range to [0, 1]"""
    img -= np.min(img)
    img /= np.max(img)
    return img


def prepare_im(load_dir, image_size, device):
    ref_img = torch.from_numpy(normalize_np(plt.imread(load_dir)[:, :, :3].astype(np.float32))).to(
        device
    )
    ref_img = ref_img.permute(2, 0, 1)
    ref_img = ref_img.view(1, 3, image_size, image_size)
    ref_img = ref_img * 2 - 1
    return ref_img


def fold_unfold(img_t, kernel, stride):
    img_shape = img_t.shape
    B, C, H, W = img_shape
    print("\n----- input shape: ", img_shape)

    patches = img_t.unfold(3, kernel, stride).unfold(2, kernel, stride).permute(0, 1, 2, 3, 5, 4)

    print("\n----- patches shape:", patches.shape)
    # reshape output to match F.fold input
    patches = patches.contiguous().view(B, C, -1, kernel * kernel)
    print("\n", patches.shape)  # [B, C, nb_patches_all, kernel_size*kernel_size]
    patches = patches.permute(0, 1, 3, 2)
    print("\n", patches.shape)  # [B, C, kernel_size*kernel_size, nb_patches_all]
    patches = patches.contiguous().view(B, C * kernel * kernel, -1)
    print("\n", patches.shape)  # [B, C*prod(kernel_size), L] as expected by Fold

    output = F.fold(patches, output_size=(H, W), kernel_size=kernel, stride=stride)
    # mask that mimics the original folding:
    recovery_mask = F.fold(
        torch.ones_like(patches), output_size=(H, W), kernel_size=kernel, stride=stride
    )
    output = output / recovery_mask

    return patches, output


def reshape_patch(x, crop_size=128, dim_size=3):
    x = x.transpose(0, 2).squeeze()  # [9, 3*(128**2)]
    x = x.view(dim_size**2, 3, crop_size, crop_size)
    return x


def reshape_patch_back(x, crop_size=128, dim_size=3):
    x = x.view(dim_size**2, 3 * (crop_size**2)).unsqueeze(dim=-1)
    x = x.transpose(0, 2)
    return x


def center_crop(img, new_width=None, new_height=None):
    width = img.shape[1]
    height = img.shape[0]

    if new_width is None:
        new_width = min(width, height)

    if new_height is None:
        new_height = min(width, height)

    left = int(np.ceil((width - new_width) / 2))
    right = width - int(np.floor((width - new_width) / 2))

    top = int(np.ceil((height - new_height) / 2))
    bottom = height - int(np.floor((height - new_height) / 2))

    if len(img.shape) == 2:
        center_cropped_img = img[top:bottom, left:right]
    else:
        center_cropped_img = img[top:bottom, left:right, ...]

    return center_cropped_img


def random_sq_bbox(img, mask_shape, image_size=256, margin=(16, 16)):
    """Generate a random sqaure mask for inpainting"""
    B, C, H, W = img.shape
    h, w = mask_shape
    margin_height, margin_width = margin
    maxt = image_size - margin_height - h
    maxl = image_size - margin_width - w

    # bb
    t = np.random.randint(margin_height, maxt)
    l = np.random.randint(margin_width, maxl)

    # make mask
    mask = torch.ones([B, C, H, W], device=img.device)
    mask[..., t : t + h, l : l + w] = 0

    return mask, t, t + h, l, l + w


def unnormalize(img, s=0.95):
    scaling = torch.quantile(img.abs(), s)
    return img / scaling


def normalize(img, s=0.95):
    scaling = torch.quantile(img.abs(), s)
    return img * scaling


# Change the values of tensor x from range [0, 1] to [-1, 1]
def normalize_center(x):
    return x.mul_(2).add_(-1)


def dynamic_thresholding(img, s=0.95):
    img = normalize(img, s=s)
    return torch.clip(img, -1.0, 1.0)


def get_gaussian_kernel(kernel_size=31, std=0.5):
    n = np.zeros([kernel_size, kernel_size])
    n[kernel_size // 2, kernel_size // 2] = 1
    k = scipy.ndimage.gaussian_filter(n, sigma=std)
    k = k.astype(np.float32)
    return k


def init_kernel_torch(kernel, device="cuda:0"):
    h, w = kernel.shape
    kernel = Variable(torch.from_numpy(kernel).to(device), requires_grad=True)
    kernel = kernel.view(1, 1, h, w)
    kernel = kernel.repeat(1, 3, 1, 1)
    return kernel


def map2tensor(gray_map):
    """Move gray maps to GPU, no normalization is done"""
    return torch.FloatTensor(gray_map).unsqueeze(0).unsqueeze(0).cuda()


def create_penalty_mask(k_size, penalty_scale):
    """Generate a mask of weights penalizing values close to the boundaries"""
    center_size = k_size // 2 + k_size % 2
    mask = create_gaussian(size=k_size, sigma1=k_size, is_tensor=False)
    mask = 1 - mask / np.max(mask)
    margin = (k_size - center_size) // 2 - 1
    mask[margin:-margin, margin:-margin] = 0
    return penalty_scale * mask


def create_gaussian(size, sigma1, sigma2=-1, is_tensor=False):
    """Return a Gaussian"""
    func1 = [
        np.exp(-(z**2) / (2 * sigma1**2)) / np.sqrt(2 * np.pi * sigma1**2)
        for z in range(-size // 2 + 1, size // 2 + 1)
    ]
    func2 = (
        func1
        if sigma2 == -1
        else [
            np.exp(-(z**2) / (2 * sigma2**2)) / np.sqrt(2 * np.pi * sigma2**2)
            for z in range(-size // 2 + 1, size // 2 + 1)
        ]
    )
    return torch.FloatTensor(np.outer(func1, func2)).cuda() if is_tensor else np.outer(func1, func2)


def total_variation_loss(img, weight):
    tv_h = ((img[:, :, 1:, :] - img[:, :, :-1, :]).pow(2)).mean()
    tv_w = ((img[:, :, :, 1:] - img[:, :, :, :-1]).pow(2)).mean()
    return weight * (tv_h + tv_w)


def pil_loader(path):
    # open path as file to avoid ResourceWarning (https://github.com/python-pillow/Pillow/issues/835)
    with open(path, "rb") as f:
        img = Image.open(f)
        return img.convert("RGB")


def default_loader(path):
    return pil_loader(path)


def tensor_img_to_npimg(tensor_img):
    """
    Turn a tensor image with shape CxHxW to a numpy array image with shape HxWxC
    :param tensor_img:
    :return: a numpy array image with shape HxWxC
    """
    if not (torch.is_tensor(tensor_img) and tensor_img.ndimension() == 3):
        raise NotImplementedError(
            "Not supported tensor image. Only tensors with dimension CxHxW are supported."
        )
    npimg = np.transpose(tensor_img.numpy(), (1, 2, 0))
    npimg = npimg.squeeze()
    assert isinstance(npimg, np.ndarray) and (npimg.ndim in {2, 3})
    return npimg


def same_padding(images, ksizes, strides, rates):
    assert len(images.size()) == 4
    batch_size, channel, rows, cols = images.size()
    out_rows = (rows + strides[0] - 1) // strides[0]
    out_cols = (cols + strides[1] - 1) // strides[1]
    effective_k_row = (ksizes[0] - 1) * rates[0] + 1
    effective_k_col = (ksizes[1] - 1) * rates[1] + 1
    padding_rows = max(0, (out_rows - 1) * strides[0] + effective_k_row - rows)
    padding_cols = max(0, (out_cols - 1) * strides[1] + effective_k_col - cols)
    # Pad the input
    padding_top = int(padding_rows / 2.0)
    padding_left = int(padding_cols / 2.0)
    padding_bottom = padding_rows - padding_top
    padding_right = padding_cols - padding_left
    paddings = (padding_left, padding_right, padding_top, padding_bottom)
    images = torch.nn.ZeroPad2d(paddings)(images)
    return images


def extract_image_patches(images, ksizes, strides, rates, padding="same"):
    """
    Extract patches from images and put them in the C output dimension.
    :param padding:
    :param images: [batch, channels, in_rows, in_cols]. A 4-D Tensor with shape
    :param ksizes: [ksize_rows, ksize_cols]. The size of the sliding window for
     each dimension of images
    :param strides: [stride_rows, stride_cols]
    :param rates: [dilation_rows, dilation_cols]
    :return: A Tensor
    """
    assert len(images.size()) == 4
    assert padding in ["same", "valid"]
    batch_size, channel, height, width = images.size()

    if padding == "same":
        images = same_padding(images, ksizes, strides, rates)
    elif padding == "valid":
        pass
    else:
        raise NotImplementedError(
            f'Unsupported padding type: {padding}.\
                Only "same" or "valid" are supported.'
        )

    unfold = torch.nn.Unfold(kernel_size=ksizes, dilation=rates, padding=0, stride=strides)
    patches = unfold(images)
    return patches  # [N, C*k*k, L], L is the total number of such blocks


def random_bbox(config, batch_size):
    """Generate a random tlhw with configuration.

    Args:
        config: Config should have configuration including img

    Returns:
        tuple: (top, left, height, width)

    """
    img_height, img_width, _ = config["image_shape"]
    h, w = config["mask_shape"]
    margin_height, margin_width = config["margin"]
    maxt = img_height - margin_height - h
    maxl = img_width - margin_width - w
    bbox_list = []
    if config["mask_batch_same"]:
        t = np.random.randint(margin_height, maxt)
        l = np.random.randint(margin_width, maxl)
        bbox_list.append((t, l, h, w))
        bbox_list = bbox_list * batch_size
    else:
        for i in range(batch_size):
            t = np.random.randint(margin_height, maxt)
            l = np.random.randint(margin_width, maxl)
            bbox_list.append((t, l, h, w))

    return torch.tensor(bbox_list, dtype=torch.int64)


def bbox2mask(bboxes, height, width, max_delta_h, max_delta_w):
    batch_size = bboxes.size(0)
    mask = torch.zeros((batch_size, 1, height, width), dtype=torch.float32)
    for i in range(batch_size):
        bbox = bboxes[i]
        delta_h = np.random.randint(max_delta_h // 2 + 1)
        delta_w = np.random.randint(max_delta_w // 2 + 1)
        mask[
            i,
            :,
            bbox[0] + delta_h : bbox[0] + bbox[2] - delta_h,
            bbox[1] + delta_w : bbox[1] + bbox[3] - delta_w,
        ] = 1.0
    return mask


def local_patch(x, bbox_list):
    assert len(x.size()) == 4
    patches = []
    for i, bbox in enumerate(bbox_list):
        t, l, h, w = bbox
        patches.append(x[i, :, t : t + h, l : l + w])
    return torch.stack(patches, dim=0)


def mask_image(x, bboxes, config):
    height, width, _ = config["image_shape"]
    max_delta_h, max_delta_w = config["max_delta_shape"]
    mask = bbox2mask(bboxes, height, width, max_delta_h, max_delta_w)
    if x.is_cuda:
        mask = mask.cuda()

    if config["mask_type"] == "hole":
        result = x * (1.0 - mask)
    elif config["mask_type"] == "mosaic":
        # TODO: Matching the mosaic patch size and the mask size
        mosaic_unit_size = config["mosaic_unit_size"]
        downsampled_image = F.interpolate(x, scale_factor=1.0 / mosaic_unit_size, mode="nearest")
        upsampled_image = F.interpolate(downsampled_image, size=(height, width), mode="nearest")
        result = upsampled_image * mask + x * (1.0 - mask)
    else:
        raise NotImplementedError("Not implemented mask type.")

    return result, mask


def spatial_discounting_mask(config):
    """Generate spatial discounting mask constant.

    Spatial discounting mask is first introduced in publication:
        Generative Image Inpainting with Contextual Attention, Yu et al.

    Args:
        config: Config should have configuration including HEIGHT, WIDTH,
            DISCOUNTED_MASK.

    Returns:
        tf.Tensor: spatial discounting mask

    """
    gamma = config["spatial_discounting_gamma"]
    height, width = config["mask_shape"]
    shape = [1, 1, height, width]
    if config["discounted_mask"]:
        mask_values = np.ones((height, width))
        for i in range(height):
            for j in range(width):
                mask_values[i, j] = max(gamma ** min(i, height - i), gamma ** min(j, width - j))
        mask_values = np.expand_dims(mask_values, 0)
        mask_values = np.expand_dims(mask_values, 0)
    else:
        mask_values = np.ones(shape)
    spatial_discounting_mask_tensor = torch.tensor(mask_values, dtype=torch.float32)
    if config["cuda"]:
        spatial_discounting_mask_tensor = spatial_discounting_mask_tensor.cuda()
    return spatial_discounting_mask_tensor


def reduce_mean(x, axis=None, keepdim=False):
    if not axis:
        axis = range(len(x.shape))
    for i in sorted(axis, reverse=True):
        x = torch.mean(x, dim=i, keepdim=keepdim)
    return x


def reduce_std(x, axis=None, keepdim=False):
    if not axis:
        axis = range(len(x.shape))
    for i in sorted(axis, reverse=True):
        x = torch.std(x, dim=i, keepdim=keepdim)
    return x


def reduce_sum(x, axis=None, keepdim=False):
    if not axis:
        axis = range(len(x.shape))
    for i in sorted(axis, reverse=True):
        x = torch.sum(x, dim=i, keepdim=keepdim)
    return x


def flow_to_image(flow):
    """Transfer flow map to image.
    Part of code forked from flownet.
    """
    out = []
    maxu = -999.0
    maxv = -999.0
    minu = 999.0
    minv = 999.0
    maxrad = -1
    for i in range(flow.shape[0]):
        u = flow[i, :, :, 0]
        v = flow[i, :, :, 1]
        idxunknow = (abs(u) > 1e7) | (abs(v) > 1e7)
        u[idxunknow] = 0
        v[idxunknow] = 0
        maxu = max(maxu, np.max(u))
        minu = min(minu, np.min(u))
        maxv = max(maxv, np.max(v))
        minv = min(minv, np.min(v))
        rad = np.sqrt(u**2 + v**2)
        maxrad = max(maxrad, np.max(rad))
        u = u / (maxrad + np.finfo(float).eps)
        v = v / (maxrad + np.finfo(float).eps)
        img = compute_color(u, v)
        out.append(img)
    return np.float32(np.uint8(out))


def pt_flow_to_image(flow):
    """Transfer flow map to image.
    Part of code forked from flownet.
    """
    out = []
    maxu = torch.tensor(-999)
    maxv = torch.tensor(-999)
    minu = torch.tensor(999)
    minv = torch.tensor(999)
    maxrad = torch.tensor(-1)
    if torch.cuda.is_available():
        maxu = maxu.cuda()
        maxv = maxv.cuda()
        minu = minu.cuda()
        minv = minv.cuda()
        maxrad = maxrad.cuda()
    for i in range(flow.shape[0]):
        u = flow[i, 0, :, :]
        v = flow[i, 1, :, :]
        idxunknow = (torch.abs(u) > 1e7) + (torch.abs(v) > 1e7)
        u[idxunknow] = 0
        v[idxunknow] = 0
        maxu = torch.max(maxu, torch.max(u))
        minu = torch.min(minu, torch.min(u))
        maxv = torch.max(maxv, torch.max(v))
        minv = torch.min(minv, torch.min(v))
        rad = torch.sqrt((u**2 + v**2).float()).to(torch.int64)
        maxrad = torch.max(maxrad, torch.max(rad))
        u = u / (maxrad + torch.finfo(torch.float32).eps)
        v = v / (maxrad + torch.finfo(torch.float32).eps)
        # TODO: change the following to pytorch
        img = pt_compute_color(u, v)
        out.append(img)

    return torch.stack(out, dim=0)


def highlight_flow(flow):
    """Convert flow into middlebury color code image."""
    out = []
    s = flow.shape
    for i in range(flow.shape[0]):
        img = np.ones((s[1], s[2], 3)) * 144.0
        u = flow[i, :, :, 0]
        v = flow[i, :, :, 1]
        for h in range(s[1]):
            for w in range(s[1]):
                ui = u[h, w]
                vi = v[h, w]
                img[ui, vi, :] = 255.0
        out.append(img)
    return np.float32(np.uint8(out))


def pt_highlight_flow(flow):
    """Convert flow into middlebury color code image."""
    out = []
    s = flow.shape
    for i in range(flow.shape[0]):
        img = np.ones((s[1], s[2], 3)) * 144.0
        u = flow[i, :, :, 0]
        v = flow[i, :, :, 1]
        for h in range(s[1]):
            for w in range(s[1]):
                ui = u[h, w]
                vi = v[h, w]
                img[ui, vi, :] = 255.0
        out.append(img)
    return np.float32(np.uint8(out))


def compute_color(u, v):
    h, w = u.shape
    img = np.zeros([h, w, 3])
    nanIdx = np.isnan(u) | np.isnan(v)
    u[nanIdx] = 0
    v[nanIdx] = 0
    # colorwheel = COLORWHEEL
    colorwheel = make_color_wheel()
    ncols = np.size(colorwheel, 0)
    rad = np.sqrt(u**2 + v**2)
    a = np.arctan2(-v, -u) / np.pi
    fk = (a + 1) / 2 * (ncols - 1) + 1
    k0 = np.floor(fk).astype(int)
    k1 = k0 + 1
    k1[k1 == ncols + 1] = 1
    f = fk - k0
    for i in range(np.size(colorwheel, 1)):
        tmp = colorwheel[:, i]
        col0 = tmp[k0 - 1] / 255
        col1 = tmp[k1 - 1] / 255
        col = (1 - f) * col0 + f * col1
        idx = rad <= 1
        col[idx] = 1 - rad[idx] * (1 - col[idx])
        notidx = np.logical_not(idx)
        col[notidx] *= 0.75
        img[:, :, i] = np.uint8(np.floor(255 * col * (1 - nanIdx)))
    return img


def pt_compute_color(u, v):
    h, w = u.shape
    img = torch.zeros([3, h, w])
    if torch.cuda.is_available():
        img = img.cuda()
    nanIdx = (torch.isnan(u) + torch.isnan(v)) != 0
    u[nanIdx] = 0.0
    v[nanIdx] = 0.0
    # colorwheel = COLORWHEEL
    colorwheel = pt_make_color_wheel()
    if torch.cuda.is_available():
        colorwheel = colorwheel.cuda()
    ncols = colorwheel.size()[0]
    rad = torch.sqrt((u**2 + v**2).to(torch.float32))
    a = torch.atan2(-v.to(torch.float32), -u.to(torch.float32)) / np.pi
    fk = (a + 1) / 2 * (ncols - 1) + 1
    k0 = torch.floor(fk).to(torch.int64)
    k1 = k0 + 1
    k1[k1 == ncols + 1] = 1
    f = fk - k0.to(torch.float32)
    for i in range(colorwheel.size()[1]):
        tmp = colorwheel[:, i]
        col0 = tmp[k0 - 1]
        col1 = tmp[k1 - 1]
        col = (1 - f) * col0 + f * col1
        idx = rad <= 1.0 / 255.0
        col[idx] = 1 - rad[idx] * (1 - col[idx])
        notidx = idx != 0
        col[notidx] *= 0.75
        img[i, :, :] = col * (1 - nanIdx).to(torch.float32)
    return img


def make_color_wheel():
    RY, YG, GC, CB, BM, MR = (15, 6, 4, 11, 13, 6)
    ncols = RY + YG + GC + CB + BM + MR
    colorwheel = np.zeros([ncols, 3])
    col = 0
    # RY
    colorwheel[0:RY, 0] = 255
    colorwheel[0:RY, 1] = np.transpose(np.floor(255 * np.arange(0, RY) / RY))
    col += RY
    # YG
    colorwheel[col : col + YG, 0] = 255 - np.transpose(np.floor(255 * np.arange(0, YG) / YG))
    colorwheel[col : col + YG, 1] = 255
    col += YG
    # GC
    colorwheel[col : col + GC, 1] = 255
    colorwheel[col : col + GC, 2] = np.transpose(np.floor(255 * np.arange(0, GC) / GC))
    col += GC
    # CB
    colorwheel[col : col + CB, 1] = 255 - np.transpose(np.floor(255 * np.arange(0, CB) / CB))
    colorwheel[col : col + CB, 2] = 255
    col += CB
    # BM
    colorwheel[col : col + BM, 2] = 255
    colorwheel[col : col + BM, 0] = np.transpose(np.floor(255 * np.arange(0, BM) / BM))
    col += +BM
    # MR
    colorwheel[col : col + MR, 2] = 255 - np.transpose(np.floor(255 * np.arange(0, MR) / MR))
    colorwheel[col : col + MR, 0] = 255
    return colorwheel


def pt_make_color_wheel():
    RY, YG, GC, CB, BM, MR = (15, 6, 4, 11, 13, 6)
    ncols = RY + YG + GC + CB + BM + MR
    colorwheel = torch.zeros([ncols, 3])
    col = 0
    # RY
    colorwheel[0:RY, 0] = 1.0
    colorwheel[0:RY, 1] = torch.arange(0, RY, dtype=torch.float32) / RY
    col += RY
    # YG
    colorwheel[col : col + YG, 0] = 1.0 - (torch.arange(0, YG, dtype=torch.float32) / YG)
    colorwheel[col : col + YG, 1] = 1.0
    col += YG
    # GC
    colorwheel[col : col + GC, 1] = 1.0
    colorwheel[col : col + GC, 2] = torch.arange(0, GC, dtype=torch.float32) / GC
    col += GC
    # CB
    colorwheel[col : col + CB, 1] = 1.0 - (torch.arange(0, CB, dtype=torch.float32) / CB)
    colorwheel[col : col + CB, 2] = 1.0
    col += CB
    # BM
    colorwheel[col : col + BM, 2] = 1.0
    colorwheel[col : col + BM, 0] = torch.arange(0, BM, dtype=torch.float32) / BM
    col += BM
    # MR
    colorwheel[col : col + MR, 2] = 1.0 - (torch.arange(0, MR, dtype=torch.float32) / MR)
    colorwheel[col : col + MR, 0] = 1.0
    return colorwheel


def is_image_file(filename):
    IMG_EXTENSIONS = [".jpg", ".jpeg", ".png", ".ppm", ".bmp", ".pgm", ".tif"]
    filename_lower = filename.lower()
    return any(filename_lower.endswith(extension) for extension in IMG_EXTENSIONS)


def deprocess(img):
    img = img.add_(1).div_(2)
    return img
