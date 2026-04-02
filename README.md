# Immich Accelerator

> **Alpha — use at your own risk.** Tested on Mac Mini M4 (24GB) with Immich v2.6.3 and OrbStack. Back up your Immich database before trying this.

Run Immich's compute natively on Apple Silicon. Thumbnails use the fast M-series CPU, video transcoding uses VideoToolbox hardware encoding, and ML runs on Metal GPU, Neural Engine, and CoreML.

Docker handles the lightweight parts (API server, Postgres, Redis). The accelerator runs Immich's own microservices worker natively on macOS, giving it access to hardware that Docker can't reach.

## How it works

```
Docker (lightweight)                 Native macOS (compute)
+-----------------------+           +-------------------------------+
|  immich-server (API)  |           |  Immich Accelerator           |
|  postgres             |<--------->|  +- Microservices worker      |
|  redis                |  DB+Redis |  |  +- Sharp (thumbnails)     |
|                       |           |  |  +- ffmpeg (VideoToolbox)  |
|  WORKERS_INCLUDE=api  |           |  +- ML service                |
|  ML_URL=host:3003     |           |     +- CLIP (MLX/Metal)       |
+-----------------------+           |     +- Faces (Vision/ANE)     |
                                    |     +- OCR (Vision/ANE)       |
                                    +-------------------------------+
```

The microservices worker is extracted directly from your running Immich Docker image. Always the exact same version, no source builds. The only modification is installing the macOS-native Sharp binary for image processing. Video transcoding is intercepted by a lightweight ffmpeg wrapper that remaps software encoders to VideoToolbox hardware encoders.

## What we modify (and how to undo it)

**Nothing inside Docker is modified.** We don't patch Immich, rebuild images, or replace containers. All changes are to your `docker-compose.yml` and can be reverted by removing a few lines.

| What we change | How | Reversible? | Risk |
|---------------|-----|-------------|------|
| Add env vars to docker-compose | `IMMICH_WORKERS_INCLUDE`, `IMMICH_MACHINE_LEARNING_URL`, `IMMICH_MEDIA_LOCATION` | Remove the lines | None |
| Expose Postgres/Redis ports | `5432:5432`, `6379:6379` in docker-compose | Remove the port lines | None |
| Native microservices worker | Extracted from Docker image, runs via `node` | Stop the accelerator | None |
| Native ML service | Separate Python service | Stop the accelerator | None |

**To fully revert:** Stop the accelerator, remove the env vars and port mappings from docker-compose, `docker compose up -d`. Immich is back to stock.

## Requirements

- macOS on Apple Silicon (M1/M2/M3/M4)
- Docker with Immich already running ([OrbStack](https://orbstack.dev) recommended, Docker Desktop works too)
- Node.js (`brew install node`)
- FFmpeg with VideoToolbox (`brew install ffmpeg`)
- Python 3.11+ for the ML service

## Quick start

### 1. Set up the ML service

```bash
git clone --recursive https://github.com/epheterson/immich-apple-silicon.git
cd immich-apple-silicon/ml
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Run setup

```bash
cd ..
python -m accelerator setup
```

This detects your Immich instance, extracts the server from Docker, installs the native Sharp binary, and tells you exactly what to change in your `docker-compose.yml`.

### 3. Configure Docker

The setup command prints the required changes. The key settings:

```yaml
services:
  immich-server:
    environment:
      - IMMICH_WORKERS_INCLUDE=api
      - IMMICH_MACHINE_LEARNING_URL=http://host.internal:3003  # OrbStack
      # Docker Desktop: use http://host.docker.internal:3003 instead
      - IMMICH_MEDIA_LOCATION=/your/upload/path
    volumes:
      # IMPORTANT: use the same absolute path on both sides (not the Docker default)
      - /your/upload/path:/your/upload/path
      - /your/photos:/your/photos:ro
```

Then: `docker compose up -d`

### Understanding path mapping

The native worker and Docker must agree on file paths. Immich stores paths in Postgres — if Docker writes `/usr/src/app/upload/thumb.jpg` but the native worker looks for `/Users/you/immich/upload/thumb.jpg`, things break.

The fix: `IMMICH_MEDIA_LOCATION` tells Immich where files live. Set it to the real host path (like `/Users/you/immich/upload`), and mount that same path in Docker (`-v /Users/you/immich/upload:/Users/you/immich/upload`). Now both sides see the same paths.

**New installs:** Set this from the start. The setup command detects your upload directory and tells you exactly what to use.

**Existing installs:** If you're changing from the Docker default (`/usr/src/app/upload`), Immich automatically rewrites all file paths in the database on the first restart with the new `IMMICH_MEDIA_LOCATION`. This is safe (it's Immich's own migration), but back up your database first.

**External photo libraries:** If you imported photos from an external library (e.g., NAS mount), those paths are stored as-is from when Docker scanned them. If Docker saw them at `/mnt/photos/...`, that's what's in the DB. The native worker needs to see them at the same path. For same-machine setups, mount the library with the same path on both sides (like uploads). For cross-machine setups (NAS + Mac), this requires both machines to see the library at the same path — which may require NFS/SMB mounts that match.

### 4. Start the accelerator

```bash
python -m accelerator start
```

Starts the native microservices worker and ML service. Immich's web UI works as usual. Uploads go through Docker's API, compute happens natively.

## Commands

| Command | What it does |
|---------|-------------|
| `python -m accelerator setup` | Detect Immich, extract server, configure |
| `python -m accelerator start` | Start native worker + ML |
| `python -m accelerator stop` | Stop native services |
| `python -m accelerator status` | Show what's running |
| `python -m accelerator logs [worker\|ml]` | Tail service logs |
| `python -m accelerator update` | Update to match new Immich version |
| `python -m accelerator watch` | Monitor + auto-restart on crash (for launchd) |

## Updates

The accelerator handles Immich updates automatically:

- **On every `start`:** checks the Docker container version, re-extracts if it changed
- **In `watch` mode:** checks every 5 minutes. If Watchtower or a manual `docker compose pull` updates Immich, the watchdog stops the worker, re-extracts the new server, and restarts. No manual intervention needed.
- **Manual:** `python -m accelerator update` if you prefer to control the timing

## Performance tuning

In the Immich admin UI (Administration → Jobs), tune the per-queue concurrency for your hardware. Recommended for M4 with 24GB:

| Queue | Concurrency | Why |
|-------|-------------|-----|
| Thumbnail Generation | 4 | CPU-bound (Sharp/libvips with NEON SIMD) |
| Smart Search | 2 | GPU-serialized (MLX Metal, no benefit higher) |
| Face Detection | 3 | Neural Engine (Vision framework) |
| OCR | 3 | Neural Engine (Vision framework) |
| Metadata Extraction | 4 | I/O-bound (exiftool) |
| Video Conversion | 1 | Hardware-accelerated via VideoToolbox |

Higher isn't always better — oversubscribing the CPU causes thrashing and actually reduces throughput.

## Split deployment (NAS + Mac)

For setups where Immich's Docker runs on a NAS and the Mac handles compute:

- Docker runs on the NAS (API + Postgres + Redis)
- Accelerator runs on the Mac (microservices + ML)
- Expose Postgres and Redis ports in docker-compose (not just localhost)
- Mount the NAS photo directory on the Mac via NFS or SMB

The tricky part is path consistency. The native worker on the Mac needs to see files at the same absolute paths that Docker on the NAS used. For uploads, `IMMICH_MEDIA_LOCATION` handles this. For external libraries, you may need to ensure the Mac's NFS/SMB mount path matches what Docker sees.

For example, if Docker on the NAS mounts photos at `/mnt/photos`, the Mac needs an NFS mount at `/mnt/photos` too (or you migrate the DB paths to match your Mac's mount point — see [issue #2](https://github.com/epheterson/immich-apple-silicon/issues/2) for an example of this).

This is an advanced setup. Start with same-machine (Docker + accelerator on the same Mac) first.

## ML service

The ML service is a managed fork of [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal) by [@sebastianfredette](https://github.com/sebastianfredette), included as a git submodule. It replaces Immich's Docker ML container with native macOS inference. Upstream changes are reviewed before merging.

| Task | Hardware | Framework |
|------|----------|-----------|
| CLIP embeddings | GPU (Metal) | MLX |
| Face detection | Neural Engine | Apple Vision |
| Face recognition | CPU / CoreML | InsightFace ONNX |
| OCR | Neural Engine | Apple Vision |

Contributions to the ML service are made via [upstream PRs](https://github.com/sebastianfredette/immich-ml-metal/pulls).

## Running as a service

The `watch` command monitors services and auto-restarts on crash. Use it with launchd for unattended operation:

```bash
cp launchd/com.immich.accelerator.plist ~/Library/LaunchAgents/
# Edit the plist: update WorkingDirectory to your repo path
launchctl load ~/Library/LaunchAgents/com.immich.accelerator.plist
```

The plist uses `watch` (not `start`) with `KeepAlive` so launchd restarts the monitor if it dies. The monitor in turn restarts ML and the worker if they crash.

## Safety

- **Immich's Docker image is unmodified.** No custom images, no patches.
- **The native worker runs Immich's own code.** Extracted from the Docker image, not reimplemented.
- **UPSERT-safe database writes.** The native worker uses Immich's own job pipeline with the same UPSERT logic.
- **Version-matched.** The extracted server always matches the Docker image version exactly.

## Security

- Config file (`~/.immich-accelerator/config.json`) is chmod 600
- Postgres exposed on `127.0.0.1:5432` (localhost only) by default
- Redis exposed on `127.0.0.1:6379` (localhost only) by default

## Migrating from v0.x

If you were using the previous version with the custom thumbnail worker and ffmpeg proxy:

1. Stop old services: `launchctl bootout gui/$(id -u) com.immich.thumbnail com.immich.ffmpeg-proxy`
2. Remove old plists from `~/Library/LaunchAgents/`
3. Remove `IMMICH_WORKERS_EXCLUDE` from your docker-compose (it never worked)
4. Follow the Quick Start above

## On agentic engineering

This project was built iteratively across several sessions with [Claude Code](https://claude.ai/code) (Opus 4.6). From zero knowledge of the Immich codebase to a working native accelerator, including upstream contributions to the ML service and a feature discussion with the Immich maintainers. Inspect the code yourself, use it and share it, or don't.

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.ai/code).
