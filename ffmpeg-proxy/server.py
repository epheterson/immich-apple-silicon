#!/usr/bin/env python3
"""Immich ffmpeg proxy with VideoToolbox hardware acceleration.

Intercepts ffmpeg/ffprobe calls from the Immich Docker container,
translates container paths to host paths, remaps software encoders
to VideoToolbox hardware encoders, injects hardware-accelerated
decoding, and handles WebP output (since Homebrew ffmpeg typically
lacks libwebp).

Configuration via environment variables:
    FFMPEG_PROXY_PORT   Listen port           (default: 3005)
    UPLOAD_DIR          Immich upload mount    (REQUIRED)
    PHOTOS_DIR          External photos mount  (REQUIRED)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ffmpeg-proxy] %(message)s")
log = logging.getLogger("ffmpeg-proxy")

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
PORT = int(os.environ.get("FFMPEG_PROXY_PORT", "3005"))

UPLOAD_HOST = os.environ.get("UPLOAD_DIR", "")
PHOTOS_HOST = os.environ.get("PHOTOS_DIR", "")

if not UPLOAD_HOST or not PHOTOS_HOST:
    log.error("UPLOAD_DIR and PHOTOS_DIR environment variables are required")
    sys.exit(1)

# Container-side paths as stored in the Immich database.
# Override these if your Immich Docker uses non-standard volume mounts.
CONTAINER_UPLOAD = os.environ.get("CONTAINER_UPLOAD_PATH", "/usr/src/app/upload").rstrip("/") + "/"
CONTAINER_PHOTOS = os.environ.get("CONTAINER_PHOTOS_PATH", "/mnt/photos").rstrip("/") + "/"

PATH_MAP = [
    (CONTAINER_UPLOAD, UPLOAD_HOST.rstrip("/") + "/"),
    (CONTAINER_PHOTOS, PHOTOS_HOST.rstrip("/") + "/"),
]

# Map software encoders AND codec names to VideoToolbox hardware encoders.
# Immich sends codec names (h264/hevc) not encoder names (libx264/libx265).
ENCODER_MAP = {
    "libx264": "h264_videotoolbox",
    "libx265": "hevc_videotoolbox",
    "h264": "h264_videotoolbox",
    "hevc": "hevc_videotoolbox",
}

SUBPROCESS_TIMEOUT = 600  # seconds — maximum wall-clock time for a single ffmpeg/ffprobe call
WEBP_FALLBACK_QUALITY = 75
STDERR_TAIL_BYTES = 3000  # truncate stderr to last N chars in JSON responses

# Stats tracking (thread-safe via lock, resets on Immich restart detection)
_stats = {"hw_encode": 0, "hw_decode": 0, "webp_fallback": 0, "total": 0, "errors": 0, "since": ""}
_stats_lock = threading.Lock()


def _stat_inc(key: str) -> None:
    """Thread-safe stats increment."""
    with _stats_lock:
        _stats[key] += 1


def _stats_reset() -> None:
    """Reset all counters (e.g. when Immich restarts with a fresh DB)."""
    with _stats_lock:
        for k in _stats:
            if k != "since":
                _stats[k] = 0
        _stats["since"] = time.strftime("%Y-%m-%dT%H:%M:%S")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a new thread — allows parallel ffmpeg calls."""
    daemon_threads = True


def translate_path(p: str) -> str:
    """Translate a container path to a host path using PATH_MAP."""
    for cp, hp in PATH_MAP:
        if p.startswith(cp):
            return hp + p[len(cp):]
    return p


def _find_output_path(args: list[str]) -> str | None:
    """Find the output file path (last non-flag argument)."""
    # ffmpeg convention: output path is the last argument
    if args and not args[-1].startswith("-"):
        return args[-1]
    return None


def _is_webp_output(args: list[str]) -> bool:
    """Check if the output file is WebP format."""
    out = _find_output_path(args)
    return bool(out and out.lower().endswith(".webp"))


def translate_args(args: list[str]) -> list[str]:
    """Translate paths, remap encoders, inject hardware decode."""
    new = []
    did_hw_encode = False
    did_hw_decode = False
    has_hwaccel = any(a == "-hwaccel" for a in args)
    # Only inject hwaccel for single-input commands; multi-input (subtitle burn,
    # concat, overlay) can break when VideoToolbox is applied to the wrong input.
    input_count = sum(1 for a in args if a == "-i")
    i = 0

    while i < len(args):
        a = args[i]

        # Remap video encoder to VideoToolbox
        if a in ("-c:v", "-vcodec") and i + 1 < len(args) and args[i + 1] in ENCODER_MAP:
            hw_enc = ENCODER_MAP[args[i + 1]]
            log.info(f"  HW encode: {args[i+1]} -> {hw_enc}")
            new.append(a)
            new.append(hw_enc)
            did_hw_encode = True
            i += 2
            continue

        # Inject -hwaccel videotoolbox before -i (single-input only)
        if a == "-i" and not has_hwaccel and not did_hw_decode and input_count == 1:
            log.info("  HW decode: injecting -hwaccel videotoolbox")
            new.extend(["-hwaccel", "videotoolbox"])
            did_hw_decode = True

        new.append(translate_path(a))
        i += 1

    if did_hw_encode:
        _stat_inc("hw_encode")
    if did_hw_decode:
        _stat_inc("hw_decode")

    return new


def _handle_webp_fallback(args: list[str]) -> tuple[int, bytes, bytes] | None:
    """Handle WebP output when ffmpeg lacks libwebp.

    Strategy: run ffmpeg to produce a temp JPEG, then convert to WebP via PIL.
    Returns (returncode, stdout, stderr) or None if not a WebP output.
    """
    if not _is_webp_output(args):
        return None

    out_path = translate_path(args[-1])
    if not out_path.lower().endswith(".webp"):
        return None

    # Verify path was actually translated to a host path
    if out_path == args[-1]:
        log.warning(f"  WebP fallback: output path not in PATH_MAP, skipping: {out_path}")
        return None

    # Replace output .webp with temp .jpg, keep all other args
    tmp_fd, tmp_jpg = tempfile.mkstemp(suffix=".jpg")
    os.close(tmp_fd)

    modified_args = list(args[:-1]) + [tmp_jpg]
    translated = translate_args(modified_args)
    cmd = [FFMPEG] + translated

    log.info(f"  WebP fallback: ffmpeg -> temp JPEG -> PIL WebP")

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT)
        if result.returncode != 0:
            if os.path.exists(tmp_jpg):
                os.remove(tmp_jpg)
            return (result.returncode, result.stdout, result.stderr)

        # Convert JPEG to WebP via PIL
        from PIL import Image
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with Image.open(tmp_jpg) as img:
            img.save(out_path, "WEBP", quality=WEBP_FALLBACK_QUALITY)

        _stat_inc("webp_fallback")
        log.info(f"  WebP fallback: OK -> {out_path}")

        return (0, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        log.error(f"  WebP fallback: ffmpeg timed out after {SUBPROCESS_TIMEOUT}s")
        return (1, b"", f"timeout after {SUBPROCESS_TIMEOUT}s".encode())
    except (OSError, ValueError) as e:
        log.error(f"  WebP fallback error: {e}")
        return (1, b"", str(e).encode())
    finally:
        if os.path.exists(tmp_jpg):
            os.remove(tmp_jpg)


class Handler(BaseHTTPRequestHandler):
    """HTTP handler for ffmpeg/ffprobe proxy requests."""

    def _send_json(self, code: int, data: dict) -> None:
        """Send a JSON response with the given HTTP status code."""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self) -> None:
        """Handle GET requests: /ping, /stats, /stats/reset."""
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
        elif self.path == "/stats":
            with _stats_lock:
                snapshot = dict(_stats)
            self._send_json(200, snapshot)
        elif self.path == "/stats/reset":
            _stats_reset()
            self._send_json(200, {"reset": True})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        """Handle POST requests: /ffmpeg and /ffprobe command execution."""
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            log.error(f"Bad JSON: {raw[:200]}")
            self.send_response(400)
            self.end_headers()
            return

        args = body.get("args", [])
        binary = FFMPEG if self.path == "/ffmpeg" else FFPROBE if self.path == "/ffprobe" else None
        if not binary:
            self.send_response(404)
            self.end_headers()
            return

        name = "ffmpeg" if binary == FFMPEG else "ffprobe"
        log.info(f"{name}: {len(args)} args: {' '.join(str(a)[:40] for a in args[:10])}...")
        _stat_inc("total")

        t0 = time.monotonic()

        # Handle WebP fallback for ffmpeg calls
        if binary == FFMPEG and _is_webp_output(args):
            webp_result = _handle_webp_fallback(args)
            if webp_result is not None:
                rc, stdout, stderr = webp_result
                elapsed = time.monotonic() - t0
                if rc != 0:
                    _stat_inc("errors")
                    log.warning(f"  exit {rc} ({elapsed:.1f}s): {stderr.decode(errors='replace')[-200:]}")
                else:
                    log.info(f"  done ({elapsed:.1f}s)")
                resp = {
                    "returncode": rc,
                    "stdout": stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout,
                    "stderr": stderr.decode(errors="replace")[-STDERR_TAIL_BYTES:] if isinstance(stderr, bytes) else stderr[-STDERR_TAIL_BYTES:],
                }
                self._send_json(200, resp)
                return

        translated = translate_args(args) if binary == FFMPEG else [translate_path(a) for a in args]
        cmd = [binary] + translated

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT)
            elapsed = time.monotonic() - t0
            if result.returncode != 0:
                _stat_inc("errors")
                log.warning(f"  exit {result.returncode} ({elapsed:.1f}s): {result.stderr.decode(errors='replace')[-200:]}")
            else:
                log.info(f"  done ({elapsed:.1f}s)")
            resp = {
                "returncode": result.returncode,
                "stdout": result.stdout.decode(errors="replace"),
                "stderr": result.stderr.decode(errors="replace")[-STDERR_TAIL_BYTES:],
            }
            self._send_json(200, resp)
        except subprocess.TimeoutExpired:
            _stat_inc("errors")
            elapsed = time.monotonic() - t0
            log.error(f"Timeout after {elapsed:.1f}s: {' '.join(str(a)[:30] for a in cmd[:5])}")
            self._send_json(504, {"error": f"timeout after {SUBPROCESS_TIMEOUT}s"})
        except OSError as e:
            _stat_inc("errors")
            log.error(f"OS error running {name}: {e}")
            self._send_json(500, {"error": str(e)})

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ARG002
        """Suppress default BaseHTTPRequestHandler access logging."""


if __name__ == "__main__":
    log.info(f"Starting on port {PORT}")
    log.info(f"  ffmpeg:  {FFMPEG}")
    log.info(f"  upload:  {UPLOAD_HOST}")
    log.info(f"  photos:  {PHOTOS_HOST}")
    log.info(f"  encoder map: {ENCODER_MAP}")
    # Binds to all interfaces by default because Docker containers reach the
    # host via a bridge IP (e.g. host.internal on OrbStack). If the Mac is on
    # an untrusted network, use a firewall to block port 3005 from external
    # access, or set FFMPEG_PROXY_BIND=127.0.0.1 if not using Docker.
    bind = os.environ.get("FFMPEG_PROXY_BIND", "0.0.0.0")
    log.info(f"  bind:    {bind}")
    _stats["since"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    ThreadedHTTPServer((bind, PORT), Handler).serve_forever()
