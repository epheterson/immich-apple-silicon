#!/usr/bin/env python3
"""Immich ffmpeg proxy with VideoToolbox hardware acceleration."""
import subprocess, json, os, logging
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ffmpeg-proxy] %(message)s")
log = logging.getLogger("ffmpeg-proxy")

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
PORT = int(os.environ.get("FFMPEG_PROXY_PORT", "3005"))

PATH_MAP = [
    ("/usr/src/app/upload/", "/Users/elp/docker/immich/upload/"),
    ("/mnt/photos/", "/nas/Pictures/"),
]

ENCODER_MAP = {
    "libx264": "h264_videotoolbox",
    "libx265": "hevc_videotoolbox",
}

def translate_path(p):
    for cp, hp in PATH_MAP:
        if p.startswith(cp):
            return hp + p[len(cp):]
    return p

def translate_args(args):
    new = []
    i = 0
    while i < len(args):
        if args[i] in ("-c:v", "-vcodec") and i+1 < len(args) and args[i+1] in ENCODER_MAP:
            log.info(f"  HW: {args[i+1]} -> {ENCODER_MAP[args[i+1]]}")
            new.append(args[i])
            new.append(ENCODER_MAP[args[i+1]])
            i += 2
            continue
        new.append(translate_path(args[i]))
        i += 1
    return new

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except:
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
        log.info(f"{name}: {len(args)} args: {' '.join(str(a)[:30] for a in args[:8])}...")
        
        translated = translate_args(args) if binary == FFMPEG else [translate_path(a) for a in args]
        cmd = [binary] + translated
        
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=600)
            if result.returncode != 0:
                log.warning(f"  exit {result.returncode}: {result.stderr.decode(errors='replace')[-200:]}")
            resp = {
                "returncode": result.returncode,
                "stdout": result.stdout.decode(errors="replace"),
                "stderr": result.stderr.decode(errors="replace")[-3000:],
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
        except Exception as e:
            log.error(f"Error: {e}")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
    
    def log_message(self, *a): pass

if __name__ == "__main__":
    log.info(f"Starting on port {PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
