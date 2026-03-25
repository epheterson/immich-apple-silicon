"""ThumbHash computation for Immich Apple Silicon thumbnail worker."""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from thumbhash import rgba_to_thumb_hash


def compute_thumbhash(image_path: str | Path, size: int = 100) -> bytes:
    """Compute a thumbhash for the given image.

    Opens the image, converts to RGBA, resizes to fit within size x size
    (maintaining aspect ratio), then encodes via thumbhash.

    Args:
        image_path: Path to the source image.
        size: Maximum dimension (width or height) of the thumbnail used
              for hashing. Default 100.

    Returns:
        Thumbhash as bytes (typically 19-21 bytes, matching Immich format).
    """
    with Image.open(image_path) as img:
        img = img.convert("RGBA")
        img.thumbnail((size, size))
        w, h = img.size
        rgba_flat = list(img.tobytes())
        hash_list = rgba_to_thumb_hash(w, h, rgba_flat)
        return bytes(hash_list)
