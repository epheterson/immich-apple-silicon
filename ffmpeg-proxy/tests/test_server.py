"""Unit tests for ffmpeg-proxy server.

Tests the pure logic — path translation, encoder remapping, hwaccel injection,
WebP detection — without requiring a running server, Docker, or ffmpeg binary.

Run:
    cd /Users/elp/Repos/immich-apple-silicon/ffmpeg-proxy
    python -m pytest tests/test_server.py -v
"""
import os
import sys

import pytest

# Set required env vars BEFORE importing server (it sys.exit(1)s without them).
os.environ["UPLOAD_DIR"] = "/host/upload"
os.environ["PHOTOS_DIR"] = "/host/photos"

# Add parent directory to path so we can import server
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server


@pytest.fixture(autouse=True)
def _reset_stats():
    """Reset stats counters before each test so they don't leak between tests."""
    for k in server._stats:
        if k != "since":
            server._stats[k] = 0


# ---------------------------------------------------------------------------
# translate_path
# ---------------------------------------------------------------------------
class TestTranslatePath:
    """Path translation from container mount points to host directories."""

    def test_upload_path(self):
        """Container /usr/src/app/upload/ maps to UPLOAD_DIR."""
        result = server.translate_path("/usr/src/app/upload/library/thumb.jpg")
        assert result == "/host/upload/library/thumb.jpg"

    def test_photos_path(self):
        """Container /mnt/photos/ maps to PHOTOS_DIR."""
        result = server.translate_path("/mnt/photos/2024/vacation/IMG_001.heic")
        assert result == "/host/photos/2024/vacation/IMG_001.heic"

    def test_unknown_path_passthrough(self):
        """Paths outside any known mount pass through unchanged."""
        result = server.translate_path("/tmp/something.mp4")
        assert result == "/tmp/something.mp4"

    def test_upload_root(self):
        """The mount root itself (no trailing subpath) translates correctly."""
        result = server.translate_path("/usr/src/app/upload/")
        assert result == "/host/upload/"

    def test_photos_root(self):
        """The photos mount root translates correctly."""
        result = server.translate_path("/mnt/photos/")
        assert result == "/host/photos/"

    def test_empty_string(self):
        """Empty string passes through unchanged."""
        assert server.translate_path("") == ""

    def test_partial_prefix_no_match(self):
        """A path that is a prefix substring but not a real prefix doesn't match."""
        # /usr/src/app/uploadextra/ should NOT match /usr/src/app/upload/
        result = server.translate_path("/usr/src/app/uploadextra/file.jpg")
        assert result == "/usr/src/app/uploadextra/file.jpg"

    def test_deeply_nested_upload(self):
        """Deeply nested paths under upload mount translate correctly."""
        deep = "/usr/src/app/upload/a/b/c/d/e/file.mp4"
        result = server.translate_path(deep)
        assert result == "/host/upload/a/b/c/d/e/file.mp4"

    def test_photos_dir_trailing_slash_stripped(self):
        """PHOTOS_DIR trailing slash is normalized (rstrip in PATH_MAP)."""
        # The PATH_MAP strips trailing slash from host dir and re-adds it,
        # so /host/photos + / + subpath works correctly.
        result = server.translate_path("/mnt/photos/file.mp4")
        assert result == "/host/photos/file.mp4"


# ---------------------------------------------------------------------------
# translate_args — encoder remap
# ---------------------------------------------------------------------------
class TestTranslateArgsEncoderRemap:
    """Encoder remapping from software to VideoToolbox hardware encoders."""

    def test_h264_remapped(self):
        """Codec name h264 remaps to h264_videotoolbox."""
        args = ["-i", "input.mp4", "-c:v", "h264", "output.mp4"]
        result = server.translate_args(args)
        assert "-c:v" in result
        idx = result.index("-c:v")
        assert result[idx + 1] == "h264_videotoolbox"

    def test_hevc_remapped(self):
        """Codec name hevc remaps to hevc_videotoolbox."""
        args = ["-i", "input.mp4", "-c:v", "hevc", "output.mp4"]
        result = server.translate_args(args)
        idx = result.index("-c:v")
        assert result[idx + 1] == "hevc_videotoolbox"

    def test_libx264_remapped(self):
        """Software encoder libx264 remaps to h264_videotoolbox."""
        args = ["-i", "input.mp4", "-c:v", "libx264", "output.mp4"]
        result = server.translate_args(args)
        idx = result.index("-c:v")
        assert result[idx + 1] == "h264_videotoolbox"

    def test_libx265_remapped(self):
        """Software encoder libx265 remaps to hevc_videotoolbox."""
        args = ["-i", "input.mp4", "-c:v", "libx265", "output.mp4"]
        result = server.translate_args(args)
        idx = result.index("-c:v")
        assert result[idx + 1] == "hevc_videotoolbox"

    def test_copy_not_remapped(self):
        """-c:v copy is NOT an encoder and must not be remapped."""
        args = ["-i", "input.mp4", "-c:v", "copy", "output.mp4"]
        result = server.translate_args(args)
        idx = result.index("-c:v")
        assert result[idx + 1] == "copy"

    def test_mjpeg_not_remapped(self):
        """Unknown encoder mjpeg passes through unchanged."""
        args = ["-i", "input.mp4", "-c:v", "mjpeg", "output.jpg"]
        result = server.translate_args(args)
        idx = result.index("-c:v")
        assert result[idx + 1] == "mjpeg"

    def test_vcodec_flag_remapped(self):
        """-vcodec (alternative to -c:v) also triggers remap."""
        args = ["-i", "input.mp4", "-vcodec", "h264", "output.mp4"]
        result = server.translate_args(args)
        idx = result.index("-vcodec")
        assert result[idx + 1] == "h264_videotoolbox"

    def test_vcodec_libx265_remapped(self):
        """-vcodec libx265 also triggers remap."""
        args = ["-i", "input.mp4", "-vcodec", "libx265", "output.mp4"]
        result = server.translate_args(args)
        idx = result.index("-vcodec")
        assert result[idx + 1] == "hevc_videotoolbox"

    def test_hw_encode_stat_incremented(self):
        """Stats counter hw_encode is incremented when remap occurs."""
        args = ["-i", "input.mp4", "-c:v", "h264", "output.mp4"]
        server.translate_args(args)
        assert server._stats["hw_encode"] == 1

    def test_hw_encode_stat_not_incremented_for_copy(self):
        """Stats counter hw_encode is NOT incremented for -c:v copy."""
        args = ["-i", "input.mp4", "-c:v", "copy", "output.mp4"]
        server.translate_args(args)
        assert server._stats["hw_encode"] == 0


# ---------------------------------------------------------------------------
# translate_args — hwaccel injection
# ---------------------------------------------------------------------------
class TestTranslateArgsHwaccelInjection:
    """Hardware-accelerated decoding injection before -i flag."""

    def test_injects_hwaccel_single_input(self):
        """Injects -hwaccel videotoolbox before -i for single-input commands."""
        args = ["-i", "input.mp4", "-c:v", "h264", "output.mp4"]
        result = server.translate_args(args)
        # -hwaccel videotoolbox should appear before -i
        hw_idx = result.index("-hwaccel")
        i_idx = result.index("-i")
        assert hw_idx < i_idx
        assert result[hw_idx + 1] == "videotoolbox"

    def test_no_inject_multi_input(self):
        """Does NOT inject hwaccel for multi-input commands (2+ -i flags)."""
        args = ["-i", "input1.mp4", "-i", "input2.mp4", "-c:v", "h264", "output.mp4"]
        result = server.translate_args(args)
        assert "-hwaccel" not in result

    def test_no_inject_if_already_present(self):
        """Does NOT inject if -hwaccel is already in args."""
        args = ["-hwaccel", "cuda", "-i", "input.mp4", "-c:v", "h264", "output.mp4"]
        result = server.translate_args(args)
        # Should keep the existing -hwaccel cuda, not add a second one
        hwaccel_count = sum(1 for a in result if a == "-hwaccel")
        assert hwaccel_count == 1
        idx = result.index("-hwaccel")
        assert result[idx + 1] == "cuda"

    def test_injection_position_before_i(self):
        """The injected -hwaccel videotoolbox goes immediately before -i."""
        args = ["-y", "-nostdin", "-i", "/some/input.mp4", "-c:v", "h264", "output.mp4"]
        result = server.translate_args(args)
        i_idx = result.index("-i")
        # -hwaccel should be at i_idx - 2, videotoolbox at i_idx - 1
        assert result[i_idx - 2] == "-hwaccel"
        assert result[i_idx - 1] == "videotoolbox"

    def test_hw_decode_stat_incremented(self):
        """Stats counter hw_decode is incremented when hwaccel is injected."""
        args = ["-i", "input.mp4", "-c:v", "copy", "output.mp4"]
        server.translate_args(args)
        assert server._stats["hw_decode"] == 1

    def test_hw_decode_stat_not_incremented_multi_input(self):
        """Stats counter hw_decode is NOT incremented for multi-input commands."""
        args = ["-i", "a.mp4", "-i", "b.mp4", "-c:v", "copy", "output.mp4"]
        server.translate_args(args)
        assert server._stats["hw_decode"] == 0

    def test_no_inject_three_inputs(self):
        """Three -i flags still counts as multi-input, no injection."""
        args = ["-i", "a.mp4", "-i", "b.mp4", "-i", "c.mp4", "output.mp4"]
        result = server.translate_args(args)
        assert "-hwaccel" not in result


# ---------------------------------------------------------------------------
# translate_args — path translation integration
# ---------------------------------------------------------------------------
class TestTranslateArgsPathTranslation:
    """translate_args also translates container paths within arguments."""

    def test_input_path_translated(self):
        """Input file path is translated from container to host path."""
        args = ["-i", "/usr/src/app/upload/lib/video.mp4", "-c:v", "copy", "out.mp4"]
        result = server.translate_args(args)
        i_idx = result.index("-i")
        # The -i value should be translated (accounting for injected hwaccel)
        translated_input = result[i_idx + 1]
        assert translated_input == "/host/upload/lib/video.mp4"

    def test_output_path_translated(self):
        """Output file path is translated from container to host path."""
        args = ["-i", "in.mp4", "-c:v", "copy", "/mnt/photos/out.mp4"]
        result = server.translate_args(args)
        assert result[-1] == "/host/photos/out.mp4"


# ---------------------------------------------------------------------------
# _is_webp_output / _find_output_path
# ---------------------------------------------------------------------------
class TestWebpDetection:
    """Detection of WebP output format from ffmpeg arguments."""

    def test_detects_webp(self):
        """Detects .webp extension in last argument."""
        args = ["-i", "input.mp4", "-frames:v", "1", "thumb.webp"]
        assert server._is_webp_output(args) is True

    def test_detects_webp_uppercase(self):
        """Detects .WEBP (case-insensitive)."""
        args = ["-i", "input.mp4", "thumb.WEBP"]
        assert server._is_webp_output(args) is True

    def test_mp4_not_webp(self):
        """Returns False for .mp4 output."""
        args = ["-i", "input.mp4", "-c:v", "h264", "output.mp4"]
        assert server._is_webp_output(args) is False

    def test_jpeg_not_webp(self):
        """Returns False for .jpeg output."""
        args = ["-i", "input.mp4", "thumb.jpeg"]
        assert server._is_webp_output(args) is False

    def test_mkv_not_webp(self):
        """Returns False for .mkv output."""
        args = ["-i", "input.mp4", "output.mkv"]
        assert server._is_webp_output(args) is False

    def test_last_arg_is_flag(self):
        """Returns falsy when last argument is a flag (starts with -)."""
        args = ["-i", "input.mp4", "-f", "null", "-"]
        # "-" starts with "-", so _find_output_path returns None
        assert not server._is_webp_output(args)

    def test_empty_args(self):
        """Returns falsy for empty args list."""
        assert not server._is_webp_output([])

    def test_webp_in_container_path(self):
        """Detects .webp even when path is a full container path."""
        args = ["-i", "/mnt/photos/vid.mp4", "/usr/src/app/upload/thumb/abc.webp"]
        assert server._is_webp_output(args) is True


class TestFindOutputPath:
    """Extraction of the output file path from ffmpeg arguments."""

    def test_normal_output(self):
        """Last non-flag argument is the output path."""
        args = ["-i", "in.mp4", "-c:v", "h264", "out.mp4"]
        assert server._find_output_path(args) == "out.mp4"

    def test_last_arg_is_flag(self):
        """Returns None when last argument starts with -."""
        args = ["-i", "in.mp4", "-f", "null", "-loglevel"]
        assert server._find_output_path(args) is None

    def test_empty_args(self):
        """Returns None for empty args."""
        assert server._find_output_path([]) is None

    def test_dash_as_output(self):
        """A bare '-' (stdout) is treated as a flag (starts with -)."""
        args = ["-i", "in.mp4", "-f", "rawvideo", "-"]
        assert server._find_output_path(args) is None

    def test_full_path_output(self):
        """Full absolute path is returned as output."""
        args = ["-i", "in.mp4", "/usr/src/app/upload/encoded/video.mp4"]
        assert server._find_output_path(args) == "/usr/src/app/upload/encoded/video.mp4"


# ---------------------------------------------------------------------------
# WebP fallback path validation
# ---------------------------------------------------------------------------
class TestWebpFallbackPathValidation:
    """WebP fallback behavior for paths inside and outside PATH_MAP."""

    def test_output_outside_path_map_returns_none(self):
        """Output path not in PATH_MAP is skipped (returns None with warning)."""
        # /tmp/ is not in PATH_MAP, so translate_path returns it unchanged,
        # which triggers the "output path not in PATH_MAP" guard.
        args = ["-i", "/usr/src/app/upload/vid.mp4", "/tmp/thumb.webp"]
        result = server._handle_webp_fallback(args)
        assert result is None

    def test_non_webp_returns_none(self):
        """Non-WebP output returns None immediately."""
        args = ["-i", "in.mp4", "out.mp4"]
        result = server._handle_webp_fallback(args)
        assert result is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
class TestEdgeCases:
    """Boundary conditions and unusual argument patterns."""

    def test_empty_args_translate(self):
        """translate_args handles empty args list without error."""
        result = server.translate_args([])
        assert result == []

    def test_args_only_flags(self):
        """Args with only flags (no output file) process without error."""
        args = ["-y", "-nostdin", "-loglevel", "error"]
        result = server.translate_args(args)
        # No -i flag, so no hwaccel injection, no encoder remap
        assert "-hwaccel" not in result
        # Flags pass through (none are container paths)
        assert result == ["-y", "-nostdin", "-loglevel", "error"]

    def test_c_v_at_end_no_value(self):
        """-c:v as the very last argument (missing value) doesn't crash."""
        args = ["-i", "input.mp4", "-c:v"]
        result = server.translate_args(args)
        # -c:v has no following arg, so no remap. Should not crash.
        assert "-c:v" in result

    def test_path_prefix_ambiguity(self):
        """Paths that are prefixes of each other don't cross-contaminate.

        /mnt/photos/ should not match /mnt/photos_backup/.
        """
        result = server.translate_path("/mnt/photos_backup/file.jpg")
        assert result == "/mnt/photos_backup/file.jpg"

    def test_translate_args_preserves_non_path_args(self):
        """Non-path arguments (numbers, codec names, etc.) pass through."""
        args = ["-threads", "4", "-b:v", "5M", "-i", "in.mp4", "out.mp4"]
        result = server.translate_args(args)
        assert "4" in result
        assert "5M" in result

    def test_multiple_calls_accumulate_stats(self):
        """Stats accumulate across multiple translate_args calls."""
        args1 = ["-i", "a.mp4", "-c:v", "h264", "out1.mp4"]
        args2 = ["-i", "b.mp4", "-c:v", "hevc", "out2.mp4"]
        server.translate_args(args1)
        server.translate_args(args2)
        assert server._stats["hw_encode"] == 2
        assert server._stats["hw_decode"] == 2

    def test_encoder_remap_only_after_codec_flag(self):
        """The string 'h264' in a non-codec position is NOT remapped."""
        # h264 appears as a value to -profile:v, not after -c:v
        args = ["-i", "in.mp4", "-profile:v", "h264", "-c:v", "copy", "out.mp4"]
        result = server.translate_args(args)
        # The h264 after -profile:v should remain unchanged
        profile_idx = result.index("-profile:v")
        assert result[profile_idx + 1] == "h264"

    def test_hwaccel_injected_only_once(self):
        """Even with encoder remap, -hwaccel is only injected once."""
        args = ["-i", "in.mp4", "-c:v", "h264", "out.mp4"]
        result = server.translate_args(args)
        hwaccel_count = sum(1 for a in result if a == "-hwaccel")
        assert hwaccel_count == 1
