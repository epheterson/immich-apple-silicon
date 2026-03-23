"""Tests for thumbnail.resize — GPU-accelerated image resizing."""

import os
import tempfile

import pytest

from thumbnail.resize import resize_image

# Real test image on NFS mount.
TEST_IMAGE = (
    "/nas/Pictures/iCloud/Eric/2020/09/18/"
    "62217014797__37D3FD0C-9F96-4C3E-903F-21AE1C2342AC.jpeg"
)

# Skip the whole module if the test image is not reachable (NFS down, etc.).
pytestmark = pytest.mark.skipif(
    not os.path.isfile(TEST_IMAGE),
    reason="Test image not available (NFS mount missing?)",
)


@pytest.fixture
def tmp_dir():
    """Provide a temporary directory that is cleaned up after the test."""
    d = tempfile.mkdtemp(prefix="immich_resize_test_")
    yield d
    # Cleanup
    for f in os.listdir(d):
        os.remove(os.path.join(d, f))
    os.rmdir(d)


class TestResizeJPEG:
    """JPEG output path (fully GPU via CGImageDestination)."""

    def test_resize_produces_correct_dimensions(self, tmp_dir):
        out = os.path.join(tmp_dir, "thumb.jpg")
        w, h = resize_image(TEST_IMAGE, out, max_dim=1440, format="jpeg", quality=80)

        assert os.path.isfile(out), "Output file was not created"
        assert max(w, h) == 1440, "Longest side should be 1440, got %d" % max(w, h)
        # Original is 4032x3024 (landscape) so width should be 1440.
        assert w == 1440
        assert h == 1080
        # Sanity: file should be non-trivial.
        assert os.path.getsize(out) > 10_000, "Output file suspiciously small"


class TestResizeWebP:
    """WebP output path (GPU resize + PIL encode)."""

    def test_resize_webp_produces_file(self, tmp_dir):
        out = os.path.join(tmp_dir, "thumb.webp")
        w, h = resize_image(TEST_IMAGE, out, max_dim=720, format="webp", quality=75)

        assert os.path.isfile(out), "Output file was not created"
        assert max(w, h) == 720, "Longest side should be 720, got %d" % max(w, h)
        assert os.path.getsize(out) > 5_000, "Output file suspiciously small"


class TestResizeNoOp:
    """Image already smaller than max_dim — should pass through at original size."""

    def test_small_image_no_upscale(self, tmp_dir):
        # First create a small image to use as input.
        small_path = os.path.join(tmp_dir, "small_input.jpg")
        resize_image(TEST_IMAGE, small_path, max_dim=200, format="jpeg", quality=90)

        # Now resize with a large max_dim — should not upscale.
        out = os.path.join(tmp_dir, "should_stay_small.jpg")
        w, h = resize_image(small_path, out, max_dim=4000, format="jpeg", quality=90)

        assert max(w, h) == 200, (
            "Should not upscale; expected 200, got %d" % max(w, h)
        )
