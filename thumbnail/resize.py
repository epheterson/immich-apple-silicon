"""
GPU-accelerated image resize using Core Image + Metal on Apple Silicon.

Uses CILanczosScaleTransform for high-quality downscaling on the GPU,
and CGImageDestination for GPU-accelerated JPEG encoding.
WebP output falls back to PIL for the encode step (resize still GPU).
"""

import os
import tempfile
from typing import Tuple

from Foundation import NSURL, NSNumber
from Quartz import (
    CIImage,
    CIFilter,
    CIContext,
    CGImageDestinationCreateWithURL,
    CGImageDestinationAddImage,
    CGImageDestinationFinalize,
    kCGImageDestinationLossyCompressionQuality,
)
from PIL import Image


# Module-level Metal-backed CIContext — reused across all calls.
_ci_context = None


def _get_context():
    """Return (and lazily create) the shared Metal-backed CIContext."""
    global _ci_context
    if _ci_context is None:
        _ci_context = CIContext.contextWithOptions_(None)
    return _ci_context


def resize_image(
    input_path: str,
    output_path: str,
    max_dim: int = 1440,
    format: str = "jpeg",
    quality: int = 80,
) -> Tuple[int, int]:
    """Resize an image so its longest side equals *max_dim*.

    Parameters
    ----------
    input_path : str
        Path to source image (JPEG, HEIC, PNG, or RAW).
    output_path : str
        Destination path for the resized image.
    max_dim : int
        Target size for the longest dimension (default 1440).
    format : str
        Output format — "jpeg" or "webp" (default "jpeg").
    quality : int
        Compression quality 0-100 (default 80).

    Returns
    -------
    (width, height) : tuple[int, int]
        Pixel dimensions of the output image.
    """
    format = format.lower()
    if format not in ("jpeg", "webp"):
        raise ValueError("format must be 'jpeg' or 'webp', got %r" % format)

    # --- Load via CIImage ---------------------------------------------------
    input_url = NSURL.fileURLWithPath_(input_path)
    ci_image = CIImage.imageWithContentsOfURL_(input_url)
    if ci_image is None:
        raise FileNotFoundError("CIImage could not load: %s" % input_path)

    extent = ci_image.extent()
    src_w = extent.size.width
    src_h = extent.size.height

    # Compute scale so the longest side == max_dim.
    longest = max(src_w, src_h)
    if longest <= max_dim:
        scale = 1.0
    else:
        scale = max_dim / longest

    # --- GPU resize via CILanczosScaleTransform -----------------------------
    lanczos = CIFilter.filterWithName_("CILanczosScaleTransform")
    lanczos.setDefaults()
    lanczos.setValue_forKey_(ci_image, "inputImage")
    lanczos.setValue_forKey_(NSNumber.numberWithFloat_(scale), "inputScale")
    lanczos.setValue_forKey_(NSNumber.numberWithFloat_(1.0), "inputAspectRatio")
    resized = lanczos.outputImage()

    out_extent = resized.extent()
    out_w = int(round(out_extent.size.width))
    out_h = int(round(out_extent.size.height))

    ctx = _get_context()

    # --- Write output -------------------------------------------------------
    if format == "jpeg":
        _write_jpeg(ctx, resized, output_path, quality)
    else:
        _write_webp(ctx, resized, output_path, quality)

    return (out_w, out_h)


def _write_jpeg(ctx, ci_image, output_path, quality):
    """Write CIImage to JPEG via CGImageDestination (fully GPU path)."""
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    out_url = NSURL.fileURLWithPath_(output_path)

    # Render CIImage -> CGImage
    extent = ci_image.extent()
    cg_image = ctx.createCGImage_fromRect_(ci_image, extent)
    if cg_image is None:
        raise RuntimeError("Failed to render CGImage from CIImage")

    dest = CGImageDestinationCreateWithURL(out_url, "public.jpeg", 1, None)
    if dest is None:
        raise RuntimeError("Failed to create CGImageDestination for %s" % output_path)

    props = {kCGImageDestinationLossyCompressionQuality: quality / 100.0}
    CGImageDestinationAddImage(dest, cg_image, props)
    ok = CGImageDestinationFinalize(dest)
    if not ok:
        raise RuntimeError("CGImageDestinationFinalize failed for %s" % output_path)


def _write_webp(ctx, ci_image, output_path, quality):
    """Render via temp JPEG then convert to WebP with PIL."""
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
    os.close(tmp_fd)
    try:
        _write_jpeg(ctx, ci_image, tmp_path, quality=95)
        with Image.open(tmp_path) as img:
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            img.save(output_path, "WEBP", quality=quality)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
