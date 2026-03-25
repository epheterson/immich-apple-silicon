"""Tests for thumbnail.resize -- GPU-accelerated image resizing."""

import os
import tempfile

import pytest

from thumbnail.resize import resize_image, generate_all

# Set TEST_IMAGE_PATH to a JPEG file to run these tests.
TEST_IMAGE = os.environ.get("TEST_IMAGE_PATH", "")

# Skip the whole module if no test image is configured or reachable.
pytestmark = pytest.mark.skipif(
    not TEST_IMAGE or not os.path.isfile(TEST_IMAGE),
    reason="Set TEST_IMAGE_PATH env var to a JPEG file to run resize tests",
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
        w, h = resize_image(TEST_IMAGE, out, max_dim=1440, output_format="jpeg", quality=80)

        assert os.path.isfile(out), "Output file was not created"
        assert max(w, h) == 1440, "Longest side should be 1440, got %d" % max(w, h)
        assert w > 0 and h > 0, "Dimensions should be positive"
        # Sanity: file should be non-trivial.
        assert os.path.getsize(out) > 10_000, "Output file suspiciously small"


class TestResizeWebP:
    """WebP output path (GPU resize + PIL encode)."""

    def test_resize_webp_produces_file(self, tmp_dir):
        out = os.path.join(tmp_dir, "thumb.webp")
        w, h = resize_image(TEST_IMAGE, out, max_dim=720, output_format="webp", quality=75)

        assert os.path.isfile(out), "Output file was not created"
        assert max(w, h) == 720, "Longest side should be 720, got %d" % max(w, h)
        assert os.path.getsize(out) > 5_000, "Output file suspiciously small"


class TestResizeNoOp:
    """Image already smaller than max_dim — should pass through at original size."""

    def test_small_image_no_upscale(self, tmp_dir):
        # First create a small image to use as input.
        small_path = os.path.join(tmp_dir, "small_input.jpg")
        resize_image(TEST_IMAGE, small_path, max_dim=200, output_format="jpeg", quality=90)

        # Now resize with a large max_dim — should not upscale.
        out = os.path.join(tmp_dir, "should_stay_small.jpg")
        w, h = resize_image(small_path, out, max_dim=4000, output_format="jpeg", quality=90)

        assert max(w, h) == 200, (
            "Should not upscale; expected 200, got %d" % max(w, h)
        )


class TestGenerateAll:
    """generate_all() — preview JPEG + thumbnail WebP + thumbhash in one pass."""

    def test_produces_both_files(self, tmp_dir):
        preview = os.path.join(tmp_dir, "preview.jpg")
        thumb = os.path.join(tmp_dir, "thumb.webp")
        generate_all(TEST_IMAGE, preview, thumb)

        assert os.path.isfile(preview), "Preview JPEG was not created"
        assert os.path.isfile(thumb), "Thumbnail WebP was not created"

    def test_preview_dimensions(self, tmp_dir):
        preview = os.path.join(tmp_dir, "preview.jpg")
        thumb = os.path.join(tmp_dir, "thumb.webp")
        pw, ph, _, _, _ = generate_all(TEST_IMAGE, preview, thumb)

        assert max(pw, ph) == 1440, (
            "Preview longest side should be 1440, got %d" % max(pw, ph)
        )
        assert pw > 0 and ph > 0, "Preview dimensions should be positive"

    def test_thumbnail_dimensions(self, tmp_dir):
        preview = os.path.join(tmp_dir, "preview.jpg")
        thumb = os.path.join(tmp_dir, "thumb.webp")
        _, _, tw, th, _ = generate_all(TEST_IMAGE, preview, thumb)

        assert max(tw, th) == 250, (
            "Thumbnail longest side should be 250, got %d" % max(tw, th)
        )
        assert tw > 0 and th > 0, "Thumbnail dimensions should be positive"

    def test_returns_5_tuple(self, tmp_dir):
        preview = os.path.join(tmp_dir, "preview.jpg")
        thumb = os.path.join(tmp_dir, "thumb.webp")
        result = generate_all(TEST_IMAGE, preview, thumb)

        assert isinstance(result, tuple), "Should return a tuple"
        assert len(result) == 5, "Should return 5 elements, got %d" % len(result)
        pw, ph, tw, th, thumbhash = result
        assert isinstance(pw, int), "preview width should be int"
        assert isinstance(ph, int), "preview height should be int"
        assert isinstance(tw, int), "thumb width should be int"
        assert isinstance(th, int), "thumb height should be int"
        assert isinstance(thumbhash, bytes), "thumbhash should be bytes"

    def test_thumbhash_size(self, tmp_dir):
        preview = os.path.join(tmp_dir, "preview.jpg")
        thumb = os.path.join(tmp_dir, "thumb.webp")
        _, _, _, _, thumbhash = generate_all(TEST_IMAGE, preview, thumb)

        assert isinstance(thumbhash, bytes), "thumbhash should be bytes"
        assert 15 <= len(thumbhash) <= 25, (
            "thumbhash should be 15-25 bytes, got %d" % len(thumbhash)
        )

    def test_output_files_non_empty(self, tmp_dir):
        preview = os.path.join(tmp_dir, "preview.jpg")
        thumb = os.path.join(tmp_dir, "thumb.webp")
        generate_all(TEST_IMAGE, preview, thumb)

        preview_size = os.path.getsize(preview)
        thumb_size = os.path.getsize(thumb)
        assert preview_size > 10_000, (
            "Preview file suspiciously small: %d bytes" % preview_size
        )
        assert thumb_size > 1_000, (
            "Thumbnail file suspiciously small: %d bytes" % thumb_size
        )

    def test_preview_larger_than_thumbnail(self, tmp_dir):
        preview = os.path.join(tmp_dir, "preview.jpg")
        thumb = os.path.join(tmp_dir, "thumb.webp")
        generate_all(TEST_IMAGE, preview, thumb)

        preview_size = os.path.getsize(preview)
        thumb_size = os.path.getsize(thumb)
        assert preview_size > thumb_size, (
            "Preview (%d bytes) should be larger than thumbnail (%d bytes)"
            % (preview_size, thumb_size)
        )

    def test_deterministic_thumbhash(self, tmp_dir):
        preview1 = os.path.join(tmp_dir, "preview1.jpg")
        thumb1 = os.path.join(tmp_dir, "thumb1.webp")
        _, _, _, _, hash1 = generate_all(TEST_IMAGE, preview1, thumb1)

        preview2 = os.path.join(tmp_dir, "preview2.jpg")
        thumb2 = os.path.join(tmp_dir, "thumb2.webp")
        _, _, _, _, hash2 = generate_all(TEST_IMAGE, preview2, thumb2)

        assert hash1 == hash2, "Thumbhash should be deterministic across calls"
