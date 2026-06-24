# Immich Accelerator

> **Alpha — use at your own risk.** Tested on Mac Mini M4 (24GB) with Immich v2.7.2 and OrbStack. Back up your Immich database before trying this.

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
| `/build` symlink (Immich 2.7+) | `/etc/synthetic.d/immich-accelerator` — requires sudo once during setup | `immich-accelerator uninstall` removes it; reboot to deactivate | Low |

**Why `/build`?** Immich 2.7+ stores absolute plugin paths like `/build/corePlugin/dist/plugin.wasm` in its database. Both Docker and native workers need `/build` to resolve. macOS SIP prevents creating root-level directories, so we use Apple's [synthetic link](https://man.cx/synthetic.conf(5)) mechanism to map `/build` → `~/.immich-accelerator/build-data`. Setup prompts for sudo once; a reboot may be required to activate.

**To fully revert:** Stop the accelerator, remove the env vars and port mappings from docker-compose, `docker compose up -d`. Immich is back to stock.

## Requirements

- macOS on Apple Silicon (M1/M2/M3/M4)
- [Homebrew](https://brew.sh)

That's it. Setup installs everything else (Docker, Node.js, ffmpeg, ML dependencies).

## Quick start

```bash
brew install epheterson/immich-accelerator/immich-accelerator
brew trust epheterson/immich-accelerator  # Homebrew 5.1.15+: lets brew upgrade see future releases
immich-accelerator setup
```

If no Docker is found, setup offers to install [OrbStack](https://orbstack.dev). If no Immich is running, setup creates the entire Docker stack for you — just answer two questions:

1. **Where are your photos?** (e.g., `~/Pictures`) — mounted read-only for Immich to import
2. **Where should Immich store its data?** (e.g., `~/.immich-accelerator/data`) — thumbnails, transcoded video, backups

Setup generates the docker-compose, starts Immich, extracts the native worker, and starts everything. Open `http://localhost:2283` to create your admin account.

For existing Immich installs, setup detects the running containers and configures the accelerator to work alongside them.

For NAS + Mac setups, see [Split deployment](#split-deployment-nas--mac) below.

### Understanding `IMMICH_MEDIA_LOCATION`

This is the directory Immich uses as its media root. It contains these subdirectories: `upload/`, `thumbs/`, `encoded-video/`, `library/`, `profile/`, `backups/`. Both Docker and the native worker must see this directory at the same absolute path. Setup handles this automatically for same-machine installs.

## Commands

| Command | What it does |
|---------|-------------|
| `immich-accelerator setup` | Auto-detect local Docker, extract server, configure |
| `immich-accelerator setup --url URL` | Setup from remote Immich instance |
| `immich-accelerator setup --manual` | Create config template for manual editing |
| `immich-accelerator start` | Start worker + ML once, foreground (testing; no auto-restart/update/log-rotation — see [Running as a service](#running-as-a-service-recommended)) |
| `immich-accelerator stop` | Stop native services |
| `immich-accelerator status` | Show what's running |
| `immich-accelerator logs [worker\|ml]` | Tail service logs |
| `immich-accelerator update` | Update to match new Immich version |
| `immich-accelerator watch` | Monitor + auto-restart/update + log rotation (what the service runs) |
| `immich-accelerator dashboard` | Web UI at http://localhost:8420 |
| `immich-accelerator ml-test` | Diagnose the ML service (health + CLIP + OCR round-trip) |
| `immich-accelerator uninstall` | Remove services, data, and launchd config |

## Dashboard

Real-time monitoring at `http://your-mac:8420`:

```bash
immich-accelerator dashboard
```

Shows service health, processing progress with live rates and ETAs, Apple Silicon hardware utilization, and system metrics. Mobile-friendly -- check from your phone.

The dashboard and setup use the Immich API for job status and queue control. Create an API key from an **admin** account in Administration > API Keys with these permissions:

| Permission | Used by | Why |
|-----------|---------|-----|
| `job.read` | Dashboard | Show queue activity (active/waiting counts) |
| `job.create` | Dashboard | Re-queue button |
| `asset.read` | Setup | Detect upload library path |
| `library.read` | Setup | Detect external library paths |

All `job.*` and `library.*` endpoints require admin access. If the dashboard shows "API key invalid," make sure the key was created by an admin user.

![Dashboard](docs/dashboard.png)

## Updates

The accelerator handles Immich updates automatically:

- **On every `start`:** checks the Docker container version, re-extracts if it changed
- **In `watch` mode:** checks every 5 minutes. If Watchtower or a manual `docker compose pull` updates Immich, the watchdog stops the worker, re-extracts the new server, and restarts. No manual intervention needed.
- **Manual:** `immich-accelerator update` if you prefer to control the timing

To update the accelerator itself:

```bash
brew upgrade immich-accelerator
```

If you run it as a service (`watch` mode — the recommended setup), that's all you need: within ~30s the watcher notices the new version on disk, relaunches itself, and restarts the worker and ML service on the new code. (A detached worker survives a plain restart, so this version-aware reload is what guarantees the new code actually takes effect — `brew services restart` alone wouldn't reload the worker.)

If you run the worker manually instead of as a service, restart it yourself after upgrading:

```bash
immich-accelerator stop && immich-accelerator start
```

If `brew upgrade` says there's nothing to do but you know a newer release exists, see [Troubleshooting](#brew-upgrade-never-finds-a-new-version).

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

## Split deployment (NAS + Mac, or any two hosts)

### The one thing you have to get right

**Both machines need to see the same files at the same absolute paths, via a shared filesystem.** The native worker reads and writes directly to disk — there is no HTTP transport of thumbnails between machines. If the NAS mounts `/volume1/photos` and the Mac mounts that same share over SMB/NFS at `/Volumes/photos`, you now have two different absolute paths for the same bytes, and Immich's database will only know one of them.

You need one absolute path that resolves to the same files on both sides. See "Two ways to get there" below.

### Topology

1. **On the NAS (or wherever Docker lives)**: Immich Docker runs server (API-only), Postgres, and Redis. Expose Postgres and Redis on the LAN (not just localhost).
2. **On the Mac**: The accelerator runs the microservices worker and ML service. Setup pulls the Immich server directly from ghcr.io — no Docker required on the Mac.

```bash
immich-accelerator setup --url http://nas:2283 --api-key YOUR_KEY
```

### Two ways to get there

**Option A — match the Mac's path inside Docker** (recommended for new installs).

Mount your Mac's shared-filesystem path on both sides with the same absolute path. Say the Mac mounts the NAS share at `/Volumes/photos`:

```yaml
# NAS docker-compose — bind the storage to the same absolute path Docker uses
volumes:
  - /volume1/photos:/Volumes/photos
environment:
  - IMMICH_MEDIA_LOCATION=/Volumes/photos
```

Docker writes `/Volumes/photos/...` to Postgres. The Mac worker opens the exact same path via its SMB/NFS mount. Same bytes, same path.

**Option B — match Docker's path on the Mac** (zero Docker changes).

Use a macOS [synthetic link](https://man.cx/synthetic.conf(5)) to make the Mac resolve Docker's internal path to your local mount:

```bash
# /etc/synthetic.d/immich-accelerator
data	Volumes/photos/immich/library
```

Reboot. Now `/data` on the Mac resolves to the SMB/NFS mount, matching what Docker already stores in the database. No `IMMICH_MEDIA_LOCATION` change needed.

> **Synthetic links can only create a single top-level name** (e.g. `/data`, `/immich`) — that's a macOS limitation, not ours. If Docker is using the container default `IMMICH_MEDIA_LOCATION=/usr/src/app/upload`, you **cannot** mirror that path on the Mac (you can't synthesize `/usr/src/app/...`, and `/usr` already exists). Use Option A, or first set `IMMICH_MEDIA_LOCATION` to a top-level path like `/data` and then synthesize that.

### Fresh split deployment: let the frontend initialize geodata first

If your Immich frontend has run **api-only from the very start** (`IMMICH_WORKERS_INCLUDE=api` set before it ever ran its own microservices worker), the reverse-geocoding tables were never initialized. The accelerator then becomes the first microservices worker to touch the database and tries to run Immich's one-time **geodata import** — a large bulk insert that can break over a network database connection (`write EPIPE`), so the worker fails to start.

Fix: initialize geodata once on the frontend, then hand off to the accelerator.

1. On the frontend, temporarily **remove** `IMMICH_WORKERS_INCLUDE=api` (or set it to include the microservices worker) and restart it.
2. Wait for it to finish the geodata import (watch its logs for "geodata import" completing).
3. **Re-add** `IMMICH_WORKERS_INCLUDE=api` and restart the frontend.
4. Start the accelerator — the tables already exist, so it skips the import.

This only affects brand-new split installs. Once geodata is initialized it stays initialized, and normal upgrades are unaffected.

### Changing IMMICH_MEDIA_LOCATION on an existing install

Immich automatically rewrites all file paths in the database on restart when `IMMICH_MEDIA_LOCATION` changes. It's safe — **but back up your database first**.

## ML service

The ML service is a managed fork of [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal) by [@sebastianfredette](https://github.com/sebastianfredette), included as a git submodule. It replaces Immich's Docker ML container with native macOS inference. Upstream changes are reviewed before merging.

| Task | Hardware | Framework |
|------|----------|-----------|
| CLIP embeddings | GPU (Metal) | MLX |
| Face detection | Neural Engine | Apple Vision |
| Face recognition | CPU / CoreML | InsightFace ONNX |
| OCR | Neural Engine | Apple Vision |

Contributions to the ML service are made via [upstream PRs](https://github.com/sebastianfredette/immich-ml-metal/pulls).

## Running as a service (recommended)

Run the accelerator as a background service, not with a bare `immich-accelerator start`. The service runs `watch` mode, and **only `watch` mode gives you**:

- **Auto-restart** — launchd (`KeepAlive`) restarts the monitor if it dies; the monitor restarts the worker, ML, and dashboard if they crash.
- **Auto-update** — picks up new Immich versions (re-extracts the worker) and notifies on accelerator updates.
- **Log rotation** — caps `worker.log`/`ml.log` so they can't grow without bound.

A plain `immich-accelerator start` runs once in the foreground with none of the above — use it only for quick testing.

**Homebrew install (recommended):**

```bash
brew services start epheterson/immich-accelerator/immich-accelerator
```

This uses the formula's own service definition, survives `brew upgrade`, and restarts at login. Check it with `brew services list`. Stop with `brew services stop epheterson/immich-accelerator/immich-accelerator`.

> Don't also install the launchd plist below if you're using `brew services` — running both double-starts the watcher.

**Git / non-Homebrew install:** `immich-accelerator setup` offers to install a launchd LaunchAgent (`~/Library/LaunchAgents/com.immich.accelerator.plist`). If you skipped that prompt, re-run `setup` and it will offer again.

Either way the service uses `watch` mode with `KeepAlive`. Confirm everything is healthy with `immich-accelerator status` and `immich-accelerator ml-test`.

## Safety

- **Immich's Docker image is unmodified.** No custom images, no patches.
- **The native worker runs Immich's own code.** Extracted from the Docker image, not reimplemented.
- **UPSERT-safe database writes.** The native worker uses Immich's own job pipeline with the same UPSERT logic.
- **Version-matched.** The extracted server always matches the Docker image version exactly.

## Known differences from Docker

The native worker runs Immich's unmodified code. The ffmpeg and image processing toolchain match Docker. The only differences are in the ML service, which uses Apple-native frameworks instead of ONNX Runtime.

| Area | Docker | Native (Accelerator) | Impact |
|------|--------|---------------------|--------|
| **ffmpeg** | Jellyfin-ffmpeg | Jellyfin-ffmpeg (same binary, macOS arm64 build) | **Identical.** Same `tonemapx` filter, same encoders, same behavior. Downloaded automatically during setup. |
| **ffmpeg encoders** | Software H.264/HEVC | VideoToolbox hardware H.264/HEVC via wrapper | Hardware-encoded output has slightly different bitstream characteristics. Visually equivalent. A lightweight wrapper remaps Immich's software encoder requests to VideoToolbox hardware equivalents. |
| **Sharp / libvips** | Prebuilt linux-arm64 Sharp | Rebuilt against Homebrew system libvips | Identical image output. System libvips handles corrupt HEIF files more gracefully (matches Docker's error handling). |
| **ML: CLIP** | ONNX Runtime | MLX on Metal GPU | Same model, different runtime. Embeddings are numerically close but not identical (floating-point differences). Search results are equivalent. |
| **ML: Face detection** | ONNX Runtime | Apple Vision framework (Neural Engine) | Different model entirely. Detection accuracy is comparable; bounding boxes may differ slightly. |
| **ML: Face recognition** | ONNX Runtime | ONNX Runtime with CoreML | Same model, CoreML acceleration. Numerically close embeddings. |
| **ML: OCR** | PaddleOCR via ONNX | Apple Vision framework (Neural Engine) | Different engine. Vision framework OCR is generally more accurate for Latin text, may differ for CJK. |
| **HEIC decode** | libvips built with libde265 | Apple ImageIO (`sips`), then Sharp | Sharp's prebuilt libvips on macOS has no HEVC decoder, so iPhone HEICs are decoded by Apple's native ImageIO before Sharp processes them. Pixels are effectively identical for thumbnails. |

### What this means in practice

- **Thumbnails, previews, and video**: Identical to Docker. Same jellyfin-ffmpeg binary, same `tonemapx` HDR tone mapping, same output. VideoToolbox hardware encoding is faster but visually equivalent.
- **HEIC photos**: Thumbnails generate correctly. The default iPhone format (HEVC-coded HEIC, often tiled) is decoded by Apple's ImageIO since Sharp's bundled libheif is AVIF-only; output is visually identical.
- **CLIP search**: Search results are equivalent but not identical. A search that returns 20 results in Docker will return ~18-20 of the same results natively, possibly in slightly different order.
- **Face grouping**: Faces are detected and grouped correctly. The grouping boundaries may differ slightly (e.g., a borderline face might be grouped differently).
- **OCR**: Text extraction is at least as good as Docker for English/Latin text.

## Troubleshooting

The accelerator will tell you what's wrong, but here are the most common friction points and the one-command fixes.

### Setup says "Upload: not detected"

Symptom — `immich-accelerator setup` finds your Immich container but reports `Upload: not detected`.

Cause — fixed in v1.5.8. Older versions only recognized uploads mounted under a `/upload` path; the modern Immich compose mounts `${UPLOAD_LOCATION}:/data` and leaves `IMMICH_MEDIA_LOCATION` unset, so detection missed it.

Fix — `brew upgrade immich-accelerator` and re-run setup. If you're on a same-machine Docker Desktop setup where the container path (`/data`) differs from the host mount, the absolute paths still have to match for the native worker to read them — see [Split deployment](#split-deployment-nas--mac); the simplest fix is to set `IMMICH_MEDIA_LOCATION` (and the bind mount) to the host path so both sides agree.

### Thumbnails 404 in the Immich web UI

Symptom — the native worker runs happily, but Immich's API server logs `ENOENT: /data/thumbs/.../xxx_thumbnail.webp` and thumbnails never show up.

Cause — split-setup path mismatch. Docker Immich stores absolute paths like `/data/library/<uuid>/...` in Postgres; the native worker writes to your `upload_mount` which is something else. Docker API then 404s the stored path.

Fix — run `immich-accelerator setup --url http://your-nas:2283 --api-key YOUR_KEY` again. v1.4.1+ detects Docker's media root via the API and refuses to save a broken config. You'll see the mismatch explicitly with both walkthroughs (match Docker, or synthetic link on Mac). See [Split deployment](#split-deployment-nas--mac) above for the two options.

### Microservices red after editing `/etc/synthetic.d/immich-accelerator` by hand

Symptom — you added your own line to `/etc/synthetic.d/immich-accelerator` (e.g. a split-deployment upload path), rebooted, and Microservices is red. The native worker won't start because `/build` doesn't resolve.

Cause — that file also holds the required `/build` synthetic link (for Immich 2.7+ plugin paths). Before v1.5.7, setup treated the file *existing* as "build link configured" and skipped writing the entry, so a hand-edited file silently lost `/build`.

Fix — upgrade to v1.5.7+ and re-run setup; it now checks for the actual `build` entry and appends it without touching your other lines. Or add it yourself and reboot:

```bash
# /etc/synthetic.d/immich-accelerator — needs a build entry (tab-separated)
printf 'build\t%s\n' "${HOME#/}/.immich-accelerator/build-data" | sudo tee -a /etc/synthetic.d/immich-accelerator
```

### ML jobs fail with "Machine learning request failed for all URLs"

Symptom — Immich's worker log shows ML requests failing with HTTP 500 on every URL, even though `immich-accelerator status` says the ML service is running.

Diagnose — run:

```bash
immich-accelerator ml-test
```

This exercises `/ping`, `/health`, CLIP visual, and OCR with a synthetic image. On any failure it tails the last 30 lines of `~/.immich-accelerator/logs/ml.log` and prints the three most common root-cause fixes. Paste the output in a GitHub issue if you're stuck.

Common causes:

- **Partial HuggingFace model cache** — `rm -rf ~/.cache/huggingface/hub/models--mlx-community--clip-vit-base-patch32` then `immich-accelerator start`
- **mlx / mlx-clip version mismatch** — `brew reinstall immich-accelerator`
- **Stale model files** — `rm -rf ~/.immich-accelerator/ml/models` then restart

### Dashboard crashes with `ModuleNotFoundError: No module named 'uvicorn'`

Fixed in v1.4.1. If you're on an older release, `brew upgrade immich-accelerator` and re-run. The formula wrapper now runs the CLI under the ML venv's Python, which has fastapi + uvicorn installed.

### `immich-accelerator setup` fails with `ENOENT: /build/corePlugin/manifest.json`

Fixed in v1.4.1. The OCI image extractor used to skip small layers that contained the Immich 2.7+ `corePlugin` WASM files. Upgrade and re-run setup.

### `brew install` fails with "Refusing to load formula ... from untrusted tap"

Homebrew 5.1.15 (June 2026) requires third-party taps to be explicitly trusted before it will load their formulas. The fix is one command:

```bash
brew trust epheterson/immich-accelerator
```

Using the fully-qualified name (`brew install epheterson/immich-accelerator/immich-accelerator`, as in the quick start) bypasses the check for that one command — Homebrew treats naming the tap explicitly as consent — but `brew upgrade` still skips the tap until it's trusted.

### `brew upgrade` never finds a new version

Symptom — `brew upgrade immich-accelerator` reports nothing to do (and `brew outdated` shows nothing), but GitHub has a newer release. `brew info immich-accelerator` shows the real error: `Refusing to load formula ... from untrusted tap`.

Cause — the same trust requirement as above, but for taps added *before* Homebrew 5.1.15 there's no error: Homebrew *silently skips* untrusted formulas during `outdated`/`upgrade`, so your install goes stale with no warning.

Fix:

```bash
brew trust epheterson/immich-accelerator
brew update && brew upgrade immich-accelerator
immich-accelerator stop && immich-accelerator start
```

## Security

- Config file (`~/.immich-accelerator/config.json`) is chmod 600
- Postgres exposed on `127.0.0.1:5432` (localhost only) by default
- Redis exposed on `127.0.0.1:6379` (localhost only) by default
- Dashboard binds on `0.0.0.0:8420` (LAN-accessible) — the Re-queue button triggers job processing via the Immich API. If you're on an untrusted network, don't run the dashboard or bind to localhost only

## On agentic engineering

This project was built iteratively across several sessions with [Claude Code](https://claude.ai/code) (Opus 4.6). From zero knowledge of the Immich codebase to a working native accelerator, including upstream contributions to the ML service and a feature discussion with the Immich maintainers. Inspect the code yourself, use it and share it, or don't.

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.ai/code).
