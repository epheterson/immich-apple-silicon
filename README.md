# immich-apple-silicon

GPU-accelerated [Immich](https://immich.app) on Apple Silicon. Replaces CPU-bound Docker processing with native macOS services using Metal GPU, Neural Engine, and VideoToolbox.

## What This Does

Immich runs great in Docker, but on Apple Silicon Macs, the GPU sits idle while the CPU maxes out. This project offloads the heavy work to native macOS services:

| Component | Docker (CPU) | Native (GPU) | Speedup |
|-----------|-------------|-------------|---------|
| **ML** (face detection, CLIP, OCR) | 58/min | 1,218/min | **21x** |
| **Video transcoding** | 434% CPU | 95% CPU (VideoToolbox) | **CPU freed** |
| **Image thumbnails** | 192/min (Sharp) | 132/min (Core Image Metal) | **CPU near 0%** |

The raw thumbnail throughput is comparable, but the key difference is **where** the work happens: GPU instead of CPU. Your Mac's CPU is free for everything else.

## Architecture

```
Mac Mini M4
├── Docker (OrbStack)
│   ├── immich-server (API only — IMMICH_WORKERS_INCLUDE=api)
│   ├── redis
│   └── postgres (ports exposed to host)
│
├── Native Services (launchd, auto-start, auto-restart)
│   ├── immich-ml-metal (port 3004)
│   │   └── Apple Vision + MLX + CoreML
│   ├── ffmpeg-proxy (port 3005)
│   │   └── VideoToolbox H.264/H.265 hardware encoding
│   └── thumbnail-worker
│       └── Core Image (Metal GPU) resize + encode
│
└── Shared Filesystem
    ├── /path/to/upload → container /usr/src/app/upload (bind mount)
    └── /path/to/photos → container /mnt/photos (NFS/SMB)
```

## Requirements

- macOS 14+ (Sonoma) on Apple Silicon (M1/M2/M3/M4)
- Python 3.11 (`brew install python@3.11`)
- An existing Immich installation (Docker)
- Immich's Postgres and Redis ports exposed to the host

## Quick Start

### 1. Install dependencies

```bash
git clone https://github.com/epheterson/immich-apple-silicon.git
cd immich-apple-silicon

# Create venv with Python 3.11
python3.11 -m venv venv
source venv/bin/activate
pip install -r thumbnail/requirements.txt
```

### 2. Configure Immich Docker

Update your `docker-compose.yml`:

```yaml
services:
  immich-server:
    environment:
      # API only — native services handle all processing
      - IMMICH_WORKERS_INCLUDE=api
      - IMMICH_MACHINE_LEARNING_URL=http://host.internal:3004
    volumes:
      # Bind mount so native services can access files
      - /path/to/upload:/usr/src/app/upload
      - /path/to/photos:/mnt/photos:ro
      # VideoToolbox ffmpeg wrappers
      - ./ffmpeg-proxy/wrappers/ffmpeg.sh:/usr/bin/ffmpeg:ro
      - ./ffmpeg-proxy/wrappers/ffprobe.sh:/usr/bin/ffprobe:ro

  redis:
    ports:
      - "6379:6379"   # Expose to host

  database:
    ports:
      - "5432:5432"   # Expose to host
```

### 3. Start native services

```bash
# ML service (face detection, CLIP embeddings, OCR)
ML_PORT=3004 ML_MAX_CONCURRENT_REQUESTS=16 python -m src.main &

# FFmpeg proxy (VideoToolbox transcoding)
python ffmpeg-proxy/server.py &

# Thumbnail worker (Core Image GPU thumbnails)
DB_HOST=localhost UPLOAD_DIR=/path/to/upload PHOTOS_DIR=/path/to/photos python -m thumbnail &
```

Or install as launchd services (auto-start on boot):

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
# Edit paths in each plist to match your setup
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.immich.ml-metal.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.immich.ffmpeg-proxy.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.immich.thumbnail.plist
```

### 4. Verify

```bash
# ML service
curl http://localhost:3004/ping  # → pong

# FFmpeg proxy
curl http://localhost:3005/ping  # → pong

# Thumbnail worker (check logs)
tail -f /tmp/immich-thumbnail.log
```

## Components

### ML Service (`ml/`)

Drop-in replacement for Immich's `immich-machine-learning` container. Based on [immich-ml-metal](https://github.com/plsnotracking/immich-ml-metal).

- **CLIP embeddings** via MLX (GPU)
- **Face detection** via Apple Vision framework (Neural Engine)
- **Face recognition** via InsightFace ArcFace with CoreML
- **OCR** via Apple Vision framework

### FFmpeg Proxy (`ffmpeg-proxy/`)

HTTP proxy that translates ffmpeg/ffprobe calls from the Docker container to native macOS ffmpeg with VideoToolbox hardware encoding.

- Translates container paths → host paths
- Remaps `libx264` → `h264_videotoolbox`, `libx265` → `hevc_videotoolbox`
- Falls back to container ffmpeg if proxy is unreachable

### Thumbnail Worker (`thumbnail/`)

Standalone service that generates Immich thumbnails using Core Image (Metal GPU).

- Polls Postgres for IMAGE assets without thumbnails
- GPU-accelerated Lanczos resize via `CILanczosScaleTransform`
- Generates preview (1440px JPEG) + thumbnail (250px WebP)
- Computes thumbhash (perceptual blur placeholder)
- Updates Immich's database directly

## Configuration

All services use environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | `localhost` | Postgres host |
| `DB_PORT` | `5432` | Postgres port |
| `DB_NAME` | `immich` | Database name |
| `DB_USER` | `postgres` | Database user |
| `DB_PASS` | `postgres` | Database password |
| `UPLOAD_DIR` | — | Host path to Immich upload directory |
| `PHOTOS_DIR` | — | Host path to photos (external library) |
| `ML_PORT` | `3003` | ML service port |
| `FFMPEG_PROXY_PORT` | `3005` | FFmpeg proxy port |
| `BATCH_SIZE` | `20` | Thumbnail worker batch size |
| `POLL_INTERVAL` | `5` | Seconds between DB polls |

## Immich Version Compatibility

Tested with Immich v2.6.1. The services interact with Immich only through:
- Postgres database (stable schema since v1.99+)
- Shared filesystem (stable path format since v1.50+)
- ML HTTP API (stable since v1.90+)

When updating Immich, check:
- [ ] `asset` table schema unchanged
- [ ] `asset_file` table schema unchanged
- [ ] Thumbnail directory structure unchanged
- [ ] Run integration tests: `python -m pytest thumbnail/tests/ -v`

## macOS Permissions

On first run, macOS may prompt for:
- **Network access** — Python connecting to localhost (Postgres/Redis)
- **File access** — Reading from NFS/external volumes
- Click **Allow** when prompted

## Credits

- [immich-ml-metal](https://github.com/plsnotracking/immich-ml-metal) — ML service foundation
- [Immich](https://immich.app) — The photo management platform
- [Jellyfin Docker macOS](https://oliverbley.github.io/posts/2022-12-27-jellyfin-in-docker-hardware-acceleration-on-macos/) — FFmpeg proxy pattern

## License

MIT
