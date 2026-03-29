# immich-apple-silicon

[![Version](https://img.shields.io/badge/version-0.1.4-blue)]()

GPU-accelerated [Immich](https://immich.app) on Apple Silicon. Offloads CPU-bound Docker processing to native macOS services using Metal GPU, Neural Engine, and VideoToolbox.

> **Alpha — use at your own risk.** This has been tested on exactly one setup (Mac Mini M4, 24GB, Immich v2.6.1, OrbStack). It works great there. Your mileage may vary. Back up your Immich database before trying this.

## How It Works

Immich runs in Docker, but Docker on macOS can't access the GPU. This project runs three small native services alongside Docker that handle the heavy processing:

```
Docker (unchanged Immich image)              Native macOS services
┌─────────────────────────────┐             ┌──────────────────────────┐
│ immich-server               │  ──HTTP──▶  │ ML service (port 3004)   │
│   "do face detection"       │             │   Apple Vision + MLX     │
│                             │             │   21x faster than Docker │
│   "transcode this video"    │             ├──────────────────────────┤
│     └─ calls /usr/bin/ffmpeg│  ──HTTP──▶  │ ffmpeg proxy (port 3005) │
│        (our wrapper script) │             │   VideoToolbox HW encode │
│                             │             ├──────────────────────────┤
│ postgres ◀────────────────────────SQL───  │ thumbnail worker         │
│   (thumbhash, asset_file)   │             │   Core Image Metal GPU   │
└─────────────────────────────┘             └──────────────────────────┘
         Shared filesystem (bind mount)
```

## What We Modify (and How to Undo It)

**Nothing inside Docker is modified.** We don't patch Immich, rebuild images, or replace containers. All changes are to your `docker-compose.yml` and can be reverted by removing a few lines.

| What we change | How | Reversible? | Risk |
|---------------|-----|-------------|------|
| Add 2 env vars to docker-compose | `IMMICH_WORKERS_EXCLUDE`, `IMMICH_MACHINE_LEARNING_URL` | Remove the lines | None |
| Mount 2 shell scripts into container | Bind mount over `/usr/bin/ffmpeg` and `/usr/bin/ffprobe` | Remove the mount lines | None — scripts fall back to container ffmpeg if proxy is down |
| Expose Postgres port to localhost | `127.0.0.1:5432:5432` in docker-compose | Remove the port line | None |
| 3 native macOS services via launchd | Standard launchd plists, auto-start on boot | `launchctl bootout` to stop, delete plist to remove | None — all use off-the-shelf tools (Python, ffmpeg, launchd) |
| **Thumbnail worker writes to Immich's DB** | UPSERTs into `asset_file`, updates `asset.thumbhash` | Stop the service; Immich can regenerate all thumbnails itself | **Medium** — if Immich changes its DB schema, the worker's queries could fail |
| Spotlight indexing suppressed | Creates `.metadata_never_index` in `thumbs/` and `encoded-video/` | Delete the files | None — prevents macOS from wasting CPU analyzing generated thumbnails |

**To fully revert:** Stop the 3 native services, restore your original docker-compose.yml, re-add the `immich-machine-learning` container, `docker compose up -d`. Immich is back to stock.

## Safety

- **Off-the-shelf tools only.** Python, Homebrew ffmpeg, macOS Core Image, launchd. No kernel extensions, no system modifications, no root access needed.
- **Immich's Docker image is unmodified.** We don't build custom images or patch Immich code.
- **The ffmpeg proxy is passthrough.** Unknown flags and arguments are passed through unchanged. If the proxy is unreachable, the wrapper falls back to the container's own ffmpeg.
- **The thumbnail worker uses UPSERT.** If it processes an asset that Immich already handled, it safely overwrites. No duplicate rows, no corruption. Immich can regenerate everything the worker has done via the admin "Generate Thumbnails" job.
- **All services are stateless.** Stop any of them at any time. No cleanup needed.

## Performance

Benchmarks on Mac Mini M4 (24GB RAM), Immich v2.6.1, photos on NFS (Synology NAS over gigabit).

| Component | Docker (baseline) | Current (v0.1, NFS) | Theoretical max (local SSD) |
|-----------|------------------|---------------------|----------------------------|
| **ML** | 58/min, CPU | 1,218/min, GPU | ~1,500/min (larger batches) |
| **Video encode** | libx264, 434% CPU | h264_videotoolbox, <1% CPU | Already at hardware max |
| **Video decode** | Software, 100% CPU | VideoToolbox hardware | Already at hardware max |
| **Thumbnails** | ~60/min, 100% CPU | **318/min, 30% CPU** | **~830/min, ~15% CPU** |

The gap between "current" and "theoretical max" for thumbnails is storage I/O. The GPU resize itself takes ~10ms per image — the rest is waiting for NFS. Users with photos on a local SSD will hit the theoretical ceiling.

## Requirements

- macOS 14+ (Sonoma) on Apple Silicon (M1/M2/M3/M4)
- Python 3.11+ with Pillow (`brew install python@3.11`)
- Homebrew ffmpeg (`brew install ffmpeg`)
- An existing Immich installation running in Docker
- **Docker runtime:** [OrbStack](https://orbstack.dev) (recommended) or Docker Desktop

For Docker Desktop users: set `FFMPEG_PROXY_URL=http://host.docker.internal:3005/ffmpeg` in your container environment. OrbStack uses `host.internal` by default.

## Quick Start

### 1. Install

```bash
git clone --recursive https://github.com/epheterson/immich-apple-silicon.git
cd immich-apple-silicon

python3.11 -m venv venv
source venv/bin/activate
pip install -r thumbnail/requirements.txt
pip install -r ml/requirements.txt
```

### 2. Configure Docker

Add these lines to your existing Immich `docker-compose.yml`:

```yaml
services:
  immich-server:
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    environment:
      - IMMICH_WORKERS_EXCLUDE=thumbnailGeneration
      - IMMICH_MACHINE_LEARNING_URL=http://host.internal:3004
    volumes:
      # Bind mount for uploads (required — native services need filesystem access)
      - /path/to/upload:/usr/src/app/upload
      # External photos via Docker SMB volume (NOT a host NFS bind mount — see note below)
      - nas-photos:/mnt/photos:ro
      # ffmpeg wrappers (intercept calls to use VideoToolbox)
      - /path/to/immich-apple-silicon/ffmpeg-proxy/wrappers/ffmpeg.sh:/usr/bin/ffmpeg:ro
      - /path/to/immich-apple-silicon/ffmpeg-proxy/wrappers/ffprobe.sh:/usr/bin/ffprobe:ro

  database:
    volumes:
      # IMPORTANT: use a bind mount, not a Docker volume.
      # Docker volumes can be lost to 'docker compose down -v' or volume pruning.
      - /path/to/immich/pgdata:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5432:5432"   # localhost only — for native thumbnail worker

# Create the SMB volume once:
#   docker volume create --driver local \
#     --opt type=cifs \
#     --opt device=//NAS_IP/share/Photos \
#     --opt "o=username=USER,password=PASS,vers=3.0,uid=1000,gid=1000" \
#     nas-photos
volumes:
  nas-photos:
    external: true
```

**Important:**
- **NAS photos must use a Docker SMB volume**, not a host NFS bind mount. NFS through OrbStack/Docker causes file handle exhaustion (`ENFILE`) during large library scans. SMB doesn't have this issue.
- **Use bind mounts for Postgres**, not Docker volumes. Docker volumes can be lost to `docker compose down -v` or `docker volume prune`. A bind mount keeps your database safe on disk.

Then `docker compose up -d` to apply.

### 2b. Recommended Immich settings

In the Immich admin UI (**Administration → Settings → Video Transcoding**), change these to avoid unnecessary re-encoding:

- **Transcode policy:** `Optimal` (default `Required` re-encodes everything, even compatible files)
- **Accepted video codecs:** Add `HEVC` and `AV1` (most iPhone videos are already HEVC — no need to transcode them)

This alone can eliminate 80-90% of your video transcoding queue.

### 3. Start native services

```bash
# ML service (face detection, CLIP embeddings, face recognition)
cd ml && python -m src.main &

# FFmpeg proxy (VideoToolbox hardware transcoding)
cd .. && UPLOAD_DIR=/path/to/upload PHOTOS_DIR=/path/to/photos python ffmpeg-proxy/server.py &

# Thumbnail worker (Core Image Metal GPU)
DB_HOST=localhost DB_PASS=YOUR_DB_PASSWORD UPLOAD_DIR=/path/to/upload PHOTOS_DIR=/path/to/photos python -m thumbnail &
```

For persistent services that auto-start on boot:

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
# Edit paths in each plist to match your setup, then:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.immich.ml-metal.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.immich.ffmpeg-proxy.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.immich.thumbnail.plist
```

### 4. Verify

```bash
curl http://localhost:3004/ping        # → pong (ML service)
curl http://localhost:3005/ping        # → pong (ffmpeg proxy)
curl http://localhost:3005/stats       # → JSON with hw_encode/hw_decode counts
tail -f /tmp/immich-thumbnail.err      # → "OK <asset-id>" lines
```

## Initial Import

If you have a large existing library (100K+ assets), the initial scan and processing will take several hours and use significant memory. This is normal.

The native services add ~2GB of overhead (ML models + Python processes) on top of Docker. On a 24GB Mac Mini, expect swap usage during the import — the system handles it, but it won't be instant. 32GB+ machines will have a smoother experience.

Tips:
- Reduce Immich job concurrency in **Administration → Settings → Job & Workers** if you see heavy swap (20GB+)
- Smart search (CLIP) and OCR are the most memory-hungry — they can be paused during import and run after thumbnails finish
- The thumbnail worker automatically backs off when available memory drops below 500MB

## Updating Immich

**All three native services survive Immich restarts and updates automatically.** No manual stop/start needed — including with Watchtower or auto-update setups.

- **FFmpeg proxy & ML service:** Stateless HTTP services. Immich reconnects after restart.
- **Thumbnail worker:** Has built-in resilience. When Postgres restarts during an update, the worker detects the connection drop, backs off with exponential retry, and auto-recovers when the database is available again. No manual intervention required.

Just update Immich however you normally do:
```bash
docker compose pull && docker compose up -d   # or let Watchtower handle it
```

**If a major Immich version changes the database schema** (rare), the thumbnail worker will detect sustained query failures, back off to 5-minute intervals, and log a clear message. To verify manually:
```bash
cd /path/to/immich-apple-silicon
python -m pytest thumbnail/tests/test_db.py -v
```

**Worst case rollback:** Remove `IMMICH_WORKERS_EXCLUDE` from docker-compose → Immich handles thumbnails itself again.

## Components

**Built by this project:**

### FFmpeg Proxy (`ffmpeg-proxy/`)

HTTP proxy that intercepts ffmpeg calls from the Docker container and runs them through native macOS ffmpeg with VideoToolbox hardware acceleration.

- Translates container paths to host paths
- Remaps software encoders to hardware: `h264`/`libx264` → `h264_videotoolbox`
- Injects `-hwaccel videotoolbox` for hardware video decoding
- WebP output fallback via PIL (Homebrew ffmpeg typically lacks libwebp)
- Falls back to container ffmpeg if proxy is unreachable
- Threaded server: handles parallel transcodes without blocking
- Stats endpoint with session tracking: `curl http://localhost:3005/stats`

### Thumbnail Worker (`thumbnail/`)

Generates Immich thumbnails using Core Image on the Metal GPU.

- Polls Postgres for IMAGE assets missing thumbnails
- Single-pass pipeline: load image once → GPU resize to both sizes → thumbhash
- Generates preview (1440px JPEG) + thumbnail (250px WebP)
- Computes thumbhash (perceptual blur placeholder)
- Persistent DB connection for throughput
- Memory-aware: pauses when available RAM drops below 500MB
- `F_NOCACHE` on source reads — prevents macOS buffer cache from filling with one-time image data
- `gc.collect()` between batches — frees GPU/PIL objects promptly
- Suppresses Spotlight indexing on output directories automatically
- Self-healing: exponential backoff on DB errors, auto-recovers after Immich restarts
- UPSERT-safe: reruns don't create duplicate data

**Integrated from the community:**

### ML Service (`ml/`) — forked from [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal)

Maintained fork of the immich-ml-metal project, included as a git submodule. Replaces Immich's Docker ML container with native macOS inference using Apple Vision (face detection), MLX (CLIP embeddings), and CoreML (face recognition). Upstream changes are reviewed before merging. 21x faster than Docker ML.

- Fixed face recognition: landmark coordinates correctly mapped through face bounding box ([upstream PR](https://github.com/sebastianfredette/immich-ml-metal/pull/3))
- Idle model unloading: CLIP and ArcFace models freed after 120s inactive (~700MB recovered)

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_HOST` | `localhost` | Postgres host |
| `DB_PORT` | `5432` | Postgres port |
| `DB_NAME` | `immich` | Database name |
| `DB_USER` | `postgres` | Database user |
| `DB_PASS` | *(required)* | Database password |
| `UPLOAD_DIR` | *(required)* | Host path to Immich upload directory |
| `PHOTOS_DIR` | *(required)* | Host path to external photo library |
| `CONTAINER_UPLOAD_PATH` | `/usr/src/app/upload` | Upload path inside the Immich Docker container |
| `CONTAINER_PHOTOS_PATH` | `/mnt/photos` | Photos path inside the Immich Docker container |
| `FFMPEG_PROXY_PORT` | `3005` | FFmpeg proxy listen port |
| `FFMPEG_PROXY_BIND` | `0.0.0.0` | FFmpeg proxy bind address |
| `BATCH_SIZE` | `20` | Thumbnail worker batch size |
| `POLL_INTERVAL` | `5` | Seconds between DB polls when idle |

## Security

The ffmpeg proxy listens on all interfaces (`0.0.0.0`) because Docker containers reach the host via a bridge IP. On untrusted networks, restrict access:

```bash
# macOS firewall
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/homebrew/bin/python3.11
```

Or set `FFMPEG_PROXY_BIND=127.0.0.1` if not using Docker.

The example `docker-compose.yml` binds Postgres to `127.0.0.1` only.

## macOS Permissions

On first run, macOS may prompt to allow network access for Python. Click **Allow**.

## Updating This Project

```bash
cd /path/to/immich-apple-silicon
git pull --recurse-submodules
# Services run from the repo directory — new code takes effect on next restart.
# Your launchd plists in ~/Library/LaunchAgents are your own copies and are not overwritten.
```

To restart services after an update:
```bash
launchctl kickstart -k gui/$(id -u)/com.immich.ffmpeg-proxy
launchctl kickstart -k gui/$(id -u)/com.immich.thumbnail
```

## Credits

- [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal) — ML service for Apple Silicon
- [Immich](https://immich.app) — The photo management platform
- [Jellyfin Docker macOS HW accel](https://oliverbley.github.io/posts/2022-12-27-jellyfin-in-docker-hardware-acceleration-on-macos/) — FFmpeg proxy pattern inspiration

## On Agentic Engineering

This project was built in one day, by one person, in one Claude Code Opus 4.6 session. After noticing inefficiency, and with zero knowledge of the codebase, today it is possible to improve the world around us and enrich the lives of others as well. Inspect and improve the codebase yourself, use it and share it, or not.

## License

MIT

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.ai/code).
