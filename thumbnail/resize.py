"""
GPU-accelerated image resize using Core Image + Metal on Apple Silicon.

Uses CILanczosScaleTransform for high-quality downscaling on the GPU,
and CGImageDestination for GPU-accelerated JPEG encoding.
WebP output falls back to PIL for the encode step (resize still GPU).
"""
from __future__ import annotations

import os
import tempfile

from Foundation import NSURL, NSNumber
from Quartz import (
    CGImageDestinationAddImage,
    CGImageDestinationCreateWithURL,
    CGImageDestinationFinalize,
    CIContext,
    CIFilter,
    CIImage,
    kCGImageDestinationLossyCompressionQuality,
)
from PIL import Image


# Module-level Metal-backed CIContext — reused across all calls.
_ci_context = None

# Intermediate JPEG quality when converting to WebP via temp file.
# High enough to avoid double-compression artifacts, low enough to keep temp small.
_WEBP_INTERMEDIATE_QUALITY = 95


def _get_context() -> CIContext:
    """Return (and lazily create) the shared Metal-backed CIContext."""
    global _ci_context
    if _ci_context is None:
        _ci_context = CIContext.contextWithOptions_(None)
    return _ci_context


def _gpu_scale(ci_image: CIImage, scale: float) -> CIImage:
    """Apply CILanczosScaleTransform on the GPU and return the scaled CIImage."""
    lanczos = CIFilter.filterWithName_("CILanczosScaleTransform")
    lanczos.setDefaults()
    lanczos.setValue_forKey_(ci_image, "inputImage")
    lanczos.setValue_forKey_(NSNumber.numberWithFloat_(scale), "inputScale")
    lanczos.setValue_forKey_(NSNumber.numberWithFloat_(1.0), "inputAspectRatio")
    return lanczos.outputImage()


def resize_image(
    input_path: str,
    output_path: str,
    max_dim: int = 1440,
    output_format: str = "jpeg",
    quality: int = 80,
) -> tuple[int, int]:
    """Resize an image so its longest side equals *max_dim*.

    Parameters
    ----------
    input_path : str
        Path to source image (JPEG, HEIC, PNG, or RAW).
    output_path : str
        Destination path for the resized image.
    max_dim : int
        Target size for the longest dimension (default 1440).
    output_format : str
        Output format — "jpeg" or "webp" (default "jpeg").
    quality : int
        Compression quality 0-100 (default 80).

    Returns
    -------
    (width, height) : tuple[int, int]
        Pixel dimensions of the output image.
    """
    output_format = output_format.lower()
    if output_format not in ("jpeg", "webp"):
        raise ValueError("format must be 'jpeg' or 'webp', got %r" % output_format)

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
    resized = _gpu_scale(ci_image, scale)

    out_extent = resized.extent()
    out_w = int(round(out_extent.size.width))
    out_h = int(round(out_extent.size.height))

    ctx = _get_context()

    # --- Write output -------------------------------------------------------
    if output_format == "jpeg":
        _write_jpeg(ctx, resized, output_path, quality)
    else:
        _write_webp(ctx, resized, output_path, quality)

    return (out_w, out_h)


def _write_jpeg(ctx: CIContext, ci_image: CIImage, output_path: str, quality: int) -> None:
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


def _write_webp(
    ctx: CIContext, ci_image: CIImage, output_path: str, quality: int,
    thumbhash_size: int = 0,
) -> bytes | None:
    """Render via temp JPEG then convert to WebP with PIL.

    If thumbhash_size > 0, computes thumbhash while the image is already
    in memory (avoids reopening the saved WebP). Returns thumbhash bytes
    or None.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
    os.close(tmp_fd)
    thumbhash = None
    try:
        _write_jpeg(ctx, ci_image, tmp_path, quality=_WEBP_INTERMEDIATE_QUALITY)
        with Image.open(tmp_path) as img:
            if thumbhash_size > 0:
                from thumbhash import rgba_to_thumb_hash
                hash_img = img.convert("RGBA")
                hash_img.thumbnail((thumbhash_size, thumbhash_size))
                hw, hh = hash_img.size
                thumbhash = bytes(rgba_to_thumb_hash(hw, hh, list(hash_img.tobytes())))

            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            img.save(output_path, "WEBP", quality=quality)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return thumbhash


def generate_all(
    input_path: str,
    preview_path: str,
    thumb_path: str,
    preview_max: int = 1440,
    thumb_max: int = 250,
    quality: int = 80,
    thumbhash_size: int = 100,
) -> tuple[int, int, int, int, bytes]:
    """Generate preview JPEG + thumbnail WebP + thumbhash in one pass.

    Loads the source image once, applies two GPU scales, and computes
    the thumbhash from the small image's pixel buffer directly — avoiding
    redundant NFS reads and temp file round-trips.

    Returns (preview_w, preview_h, thumb_w, thumb_h, thumbhash_bytes).
    """
    # --- Load source once ---
    # Hint macOS not to cache this file — we read each image exactly once
    # during import. Without this, the buffer cache fills with one-time reads
    # and pushes application pages to swap.
    try:
        import fcntl
        fd = os.open(input_path, os.O_RDONLY)
        try:
            fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        finally:
            os.close(fd)
    except OSError:
        pass

    input_url = NSURL.fileURLWithPath_(input_path)
    ci_image = CIImage.imageWithContentsOfURL_(input_url)
    if ci_image is None:
        raise FileNotFoundError("CIImage could not load: %s" % input_path)

    extent = ci_image.extent()
    src_w = extent.size.width
    src_h = extent.size.height
    longest = max(src_w, src_h)
    ctx = _get_context()

    # --- Preview (1440px JPEG, fully GPU) ---
    preview_scale = min(preview_max / longest, 1.0) if longest > 0 else 1.0
    preview_ci = _gpu_scale(ci_image, preview_scale)

    pe = preview_ci.extent()
    pw, ph = int(round(pe.size.width)), int(round(pe.size.height))
    _write_jpeg(ctx, preview_ci, preview_path, quality)

    # --- Thumbnail (250px, GPU resize → PIL WebP) ---
    thumb_scale = min(thumb_max / longest, 1.0) if longest > 0 else 1.0
    thumb_ci = _gpu_scale(ci_image, thumb_scale)

    te = thumb_ci.extent()
    tw, th_ = int(round(te.size.width)), int(round(te.size.height))

    # Write WebP via temp JPEG and compute thumbhash in the same pass
    # (the image is already in memory for the WebP conversion)
    thumbhash = _write_webp(ctx, thumb_ci, thumb_path, quality,
                            thumbhash_size=thumbhash_size)

    return (pw, ph, tw, th_, thumbhash)
