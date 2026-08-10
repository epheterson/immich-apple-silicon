# Immich Accelerator

[![Release](https://img.shields.io/github/v/release/epheterson/immich-apple-silicon.svg?label=release)](https://github.com/epheterson/immich-apple-silicon/releases)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-Apple%20Silicon-blue.svg)]()
[![Homebrew](https://img.shields.io/badge/install-Homebrew-orange.svg)](https://github.com/epheterson/homebrew-immich-accelerator)
[![Immich](https://img.shields.io/badge/Immich-2.7%2B-5b21b6.svg)](https://immich.app/)

> **Beta.** In daily use on a Mac Mini M4 (24GB) against an Immich 3.0.x library. Stable, but back up your Immich database before your first run.

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

The microservices worker is extracted directly from your running Immich Docker image, so it tracks whatever version you run (verified with Immich 2.7.x and 3.0.x). Always the exact same version, no source builds. The only modification is installing the macOS-native Sharp binary for image processing. Video transcoding is intercepted by a lightweight ffmpeg wrapper that remaps software encoders to VideoToolbox hardware encoders.

## What we modify (and how to undo it)

**Nothing inside Docker is modified.** We don't patch Immich, rebuild images, or replace containers. All changes are to your `docker-compose.yml` and can be reverted by removing a few lines.

| What we change | How | Reversible? | Risk |
|---------------|-----|-------------|------|
| Add env vars to docker-compose | `IMMICH_WORKERS_INCLUDE`, `IMMICH_MACHINE_LEARNING_URL`, `IMMICH_MEDIA_LOCATION` | Remove the lines | None |
| Expose Postgres/Redis ports | `5432:5432`, `6379:6379` in docker-compose | Remove the port lines | None |
| Native microservices worker | Extracted from Docker image, runs via `node` | Stop the accelerator | None |
| Native ML service | Native Swift engine (Python venv fallback) | Stop the accelerator | None |
| `/build` symlink (Immich 2.7+) | `/etc/synthetic.d/immich-accelerator` (requires sudo once during setup) | `immich-accelerator uninstall` removes it; reboot to deactivate | Low |

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

If no Docker is found, setup offers to install [OrbStack](https://orbstack.dev). If no Immich is running, setup creates the entire Docker stack for you. Just answer two questions:

1. **Where are your photos?** (e.g., `~/Pictures`), mounted read-only for Immich to import
2. **Where should Immich store its data?** (e.g., `~/.immich-accelerator/data`) for thumbnails, transcoded video, and backups

Setup generates the docker-compose, starts Immich, extracts the native worker, and starts everything. Open `http://localhost:2283` to create your admin account.

For existing Immich installs, setup detects the running containers and configures the accelerator to work alongside them.

For NAS + Mac setups, see [Split deployment](#split-deployment-nas--mac) below.

### Understanding `IMMICH_MEDIA_LOCATION`

This is the directory Immich uses as its media root. It contains these subdirectories: `upload/`, `thumbs/`, `encoded-video/`, `library/`, `profile/`, `backups/`. Both Docker and the native worker must see this directory at the same absolute path. Setup handles this automatically for same-machine installs.

### Putting thumbnails or transcodes on a faster disk

Immich derives every storage path from `IMMICH_MEDIA_LOCATION`, so there is no separate setting for thumbnails (this is true in Docker too). To keep the library on slow storage while thumbnails live on an SSD, symlink the subdirectory:

```bash
immich-accelerator stop
mv /path/to/media/thumbs /Volumes/fast-ssd/thumbs
ln -s /Volumes/fast-ssd/thumbs /path/to/media/thumbs
```

The same works for `encoded-video/`. Immich follows the symlink, so reads and writes land on the SSD.

If the SSD is not mounted, that symlink points nowhere and every job writing to it would fail. The accelerator checks for this at startup and refuses to start, naming the broken path, instead of letting those jobs fail one by one.

## Commands

Every command is prefixed with `immich-accelerator` (e.g. `immich-accelerator setup`).

| Command | What it does |
|---------|-------------|
| `setup` | Auto-detect local Docker, extract server, configure |
| `setup --url URL` | Set up from a remote Immich instance |
| `setup --manual` | Create a config template for manual editing |
| `setup --ml-only` | Set up this Mac as an [ML-only network compute node](#running-as-an-ml-only-network-node) |
| `start` | Run worker + ML once in the foreground (testing only; use the [service](#running-as-a-service-recommended) for auto-restart/update/log-rotation) |
| `stop` | Stop native services |
| `status` | Show what's running |
| `logs [worker\|ml]` | Tail service logs |
| `update` | Update to match a new Immich version |
| `watch` | Monitor + auto-restart/update + log rotation (what the service runs) |
| `dashboard` | Web UI at http://localhost:8420 |
| `component [name] [on\|off]` | Turn the [worker, ML, or dashboard](#choosing-what-runs) on or off (no args lists them) |
| `ml-test` | Diagnose the ML service (health + CLIP + OCR round-trip) |
| `uninstall` | Remove services, data, and launchd config |

## Dashboard

Real-time monitoring at `http://your-mac:8420`:

```bash
immich-accelerator dashboard
```

Shows service health, processing progress with live rates and ETAs, Apple Silicon hardware utilization, and system metrics. Mobile-friendly, check from your phone.

The dashboard and setup use the Immich API for job status and queue control. Create an API key from an **admin** account in Administration > API Keys with these permissions:

| Permission | Used by | Why |
|-----------|---------|-----|
| `job.read` | Dashboard | Show queue activity (active/waiting counts) |
| `job.create` | Dashboard | Re-queue button |
| `asset.read` | Setup | Detect upload library path |
| `library.read` | Setup | Detect external library paths |

All `job.*` and `library.*` endpoints require admin access. If the dashboard shows "API key invalid," make sure the key was created by an admin user.

![Dashboard](docs/dashboard.png)

## Menu bar app

A native menu-bar app shows accelerator health at a glance and covers the daily actions: worker / ML / dashboard status (with a NATIVE or PYTHON engine badge), start / stop / restart, run `ml-test` inline, open Immich, the dashboard, or logs, and launch-at-login. It reads the accelerator's own state directly (no extra services) and weighs about 90KB.

```bash
brew install --cask epheterson/immich-accelerator/immich-accelerator-menubar
open "/Applications/Immich Accelerator.app"
```

Design inspired by [Immich-Accelerator-Helper](https://github.com/pl4za/Immich-Accelerator-Helper) by [@pl4za](https://github.com/pl4za).

## Updates

The accelerator handles Immich updates automatically:

- **On every `start`:** checks the Docker container version, re-extracts if it changed
- **In `watch` mode:** checks every 5 minutes. If Watchtower or a manual `docker compose pull` updates Immich, the watchdog stops the worker, re-extracts the new server, and restarts. No manual intervention needed.
- **Manual:** `immich-accelerator update` if you prefer to control the timing

To update the accelerator itself:

```bash
brew upgrade immich-accelerator
```

If you run it as a service (`watch` mode, the recommended setup), that's all you need: within ~30s the watcher notices the new version on disk, relaunches itself, and restarts the worker and ML service on the new code. (A detached worker survives a plain restart, so this version-aware reload is what guarantees the new code actually takes effect; `brew services restart` alone wouldn't reload the worker.)

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

Higher isn't always better. Oversubscribing the CPU causes thrashing and actually reduces throughput.

The ONNX models in the CLIP zoo (anything that isn't the default MLX model) let onnxruntime use every core, which is right on a Mac doing nothing else and less obviously right while the worker is transcoding on the same machine. `IMMICH_ACCEL_ML_THREADS` caps it without touching the worker; unset means "use every core", which stays the default.

The cap is close to free, because the parallelism saturates early. On an M4 with ViT-B-16, capping to 2 measured 179ms against 175ms uncapped, for 8 fewer threads competing with the worker. Try it if search feels like it's making the rest of the machine sluggish.

## Split deployment (NAS + Mac, or any two hosts)

Run the Immich Docker stack (API, Postgres, Redis) on one host and the native accelerator (worker + ML) on another. The one rule: **both machines must see the same files at the same absolute paths via a shared filesystem.** Setup detects a path mismatch and refuses to save a broken config.

See **[docs/split-deployment.md](docs/split-deployment.md)** for the full guide: topology, the two ways to align paths (match the Mac in Docker, or a synthetic link on the Mac), fresh-install geodata initialization, and changing `IMMICH_MEDIA_LOCATION` safely.

## ML service

The ML service runs Immich's CLIP, face, and OCR inference natively on Apple Silicon.

| Task | Hardware | Framework |
|------|----------|-----------|
| CLIP embeddings (image + text) | GPU (Metal) | mlx-swift |
| Face detection + landmarks | Neural Engine | Apple Vision |
| Face recognition | CPU | InsightFace ArcFace (onnxruntime) |
| OCR | Neural Engine | Apple Vision |

As of 1.6.0 this runs as a **native Swift engine**: a single binary with the models and libraries bundled, no Python. It replaces the ~1.5 GB Python venv (torch, mlx, onnxruntime, opencv, insightface) and the dependency-pin fragility that came with it. It uses the same weights and models as the Python service, so embeddings stay in the same space as an existing Immich search index and face clusters (no re-index, no re-cluster).

As of 1.7.0 the native engine supports the **full CLIP model zoo**: whatever model you select in Immich (ViT-B-16, ViT-L-14, LAION variants, the SigLIP family, ...) is downloaded from Immich's own model repository on first use and run natively through onnxruntime with Immich's exact preprocessing and tokenization, so results match the Docker ML service. The default ViT-B-32 uses an even faster mlx path.

The full SigLIP and SigLIP2 family (17 model/resolution combinations, e.g. `ViT-SO400M-16-SigLIP2-384__webli`) also runs this faster mlx path instead of onnxruntime — 2x-5x faster on CLIP image embeddings depending on model size (bigger models see a bigger win; a fixed CPU-side resize cost dominates more of the total for smaller ones), measured with `scripts/native-ml-siglip-benchmark.py`. Every other zoo model still runs through onnxruntime as described above.

Changing the model in Immich takes effect on the next Smart Search job, not immediately: the new model is fetched the first time Immich asks for it, which for the largest models is several GB. While that download runs, the menu bar shows "Downloading model…" with progress, and Immich's search jobs fail and retry until it finishes (nothing is lost, they succeed once the model is ready). You can also follow it with `immich-accelerator logs ml`.

Note that `ml-test` always probes with `ViT-B-32__openai`, so its output does not tell you which model your library is using; it prints your configured model separately.

The native engine is the default and is health-checked at startup. If its bundle or models are missing, or it fails to start, the accelerator automatically falls back to the Python service so ML is never left down. On a brand-new install the models (~740MB) are downloaded once in the background on first native start, so ML runs on the Python engine for a few minutes until they arrive, then switches to native automatically.

**Switching back to the Python engine.** If you want to force the Python service (for example to compare results, or if native misbehaves), set `ml_engine` in `~/.immich-accelerator/config.json`:

```json
{ "ml_engine": "python" }
```

Then restart the accelerator (`brew services restart epheterson/immich-accelerator/immich-accelerator`, or `immich-accelerator stop` then `start`). Set it back to `"native"` (or remove the key) and restart to return to native. Confirm which engine is live with `immich-accelerator ml-test` and check `ml.log`.

The Python engine is a managed fork of [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal) by [@sebastianfredette](https://github.com/sebastianfredette), included as a git submodule; upstream changes are reviewed before merging, and contributions are made via [upstream PRs](https://github.com/sebastianfredette/immich-ml-metal/pulls).

## Choosing what runs

The accelerator is three separate processes, and each one can be turned off independently:

| Component | What it does | Turn it off when |
|-----------|--------------|------------------|
| `worker` | Thumbnails, video transcoding, metadata, RAW/HEIC decode | Another machine already runs the worker and you only want this Mac for ML |
| `ml` | Smart search (CLIP), face detection, OCR | Another machine already does ML and you want this Mac for thumbnails and VideoToolbox |
| `dashboard` | The web UI on port 8420 | You use the menu bar app, or you don't want a port open |

```bash
immich-accelerator component              # list what's on
immich-accelerator component ml off       # this Mac does thumbnails and video, not ML
immich-accelerator component worker off   # this Mac does ML only
```

Changes apply immediately, including to a running service, and they persist as plain keys in `~/.immich-accelerator/config.json`. The menu bar app's Settings window has the same three switches, and a component you turn off disappears from the menu rather than showing as a red failure.

**Turning `ml` off hands ML to another machine, so tell it where.** The accelerator normally points the worker at its own engine on `localhost`. With `ml` off it stops setting that, and whatever you configured in Immich under **Administration → Machine Learning Settings** applies instead. Immich's default there is `immich-machine-learning:3003`, a Docker-internal hostname a worker running natively on macOS cannot resolve, so if you never changed it every ML job will fail. Either set that URL in Immich to a reachable engine, or set `"ml_url"` in `config.json` to name it here. Toggling `ml` restarts the worker, because that URL is fixed when the worker starts.

**This goes as far as the process boundaries and no further.** Video, thumbnails, metadata and RAW decode all happen inside the single Immich microservices worker, so "video but not thumbnails" isn't something the accelerator can offer. That split is Immich's job scheduler: use **Administration → Jobs** to pause individual queues, and see [performance tuning](#performance-tuning) for concurrency.

## Running as an ML-only network node

This is the `worker off, ml on` case above, with a setup flag (`--ml-only`) that skips every Docker and database step.

Dedicate a spare Apple Silicon Mac to ML compute only — no worker, no Docker, no
Postgres, no Redis, no library mount — and point another Immich instance's stock
**Administration → Machine Learning Settings → Remote Machine Learning URL** at it:

```text
http://<this-mac-ip>:3003
```

This turns the Mac into pure ML compute for an Immich instance (or several) running
anywhere else on the network, without the shared-filesystem/path-alignment rules that
[split deployment](#split-deployment-nas--mac-or-any-two-hosts) requires — because
unlike the worker, the ML service never touches the library on disk, only the image
bytes and text sent to it over HTTP.

### Setup

```bash
immich-accelerator setup --ml-only
```

Finds (or offers to build) the Python venv fallback engine, writes a minimal config
(`"worker": false`), and offers to start now and install as a launch-at-login service —
same as a full install, minus every Docker/worker/database step. To turn this
node into a full install later, re-run `immich-accelerator setup`: the worker
needs the Docker, database and library details that ML-only setup never collects,
so `component worker on` cannot conjure them and will tell you so.

### What's different from a full install

- `start` / `watch` / `brew services start` bring up only the ML engine (native Swift by
  default, Python venv fallback) and the [dashboard](#dashboard) — no worker, no
  `/build` link, no Immich version tracking, no database connectivity checks.
- `status`, `stop`, `logs ml`, and `ml-test` all work exactly as in a full install.
- The dashboard's processing-progress bars aren't meaningful here — there's no single
  "owning" Immich library, since this node can serve any number of Immich instances —
  so it shows ML health and Apple Silicon hardware utilization only.
- Changing the CLIP/face model is still controlled entirely from each consuming
  Immich instance's own admin settings, same as any other deployment — see
  [ML service](#ml-service) above.

### Security note

The ML service binds `0.0.0.0:3003` (LAN-accessible) and, like Immich's own Docker ML
service, has **no authentication** — anything that can reach the port can submit
inference requests. This is the same trust model as Immich's stock `machine-learning`
container; ml-only mode just makes it reachable from other hosts instead of only
`localhost`. If you're on an untrusted network, put it behind a firewall or VPN and
don't expose port 3003 to the internet — same guidance as the [dashboard's security
note](#security) below.

## Running as a service (recommended)

Run the accelerator as a background service, not with a bare `immich-accelerator start`. The service runs `watch` mode, and **only `watch` mode gives you**:

- **Auto-restart**: launchd (`KeepAlive`) restarts the monitor if it dies; the monitor restarts the worker, ML, and dashboard if they crash.
- **Auto-update**: picks up new Immich versions (re-extracts the worker) and notifies on accelerator updates.
- **Log rotation**: caps `worker.log`/`ml.log` so they can't grow without bound.

A plain `immich-accelerator start` runs once in the foreground with none of the above. Use it only for quick testing.

**Homebrew install (recommended):**

```bash
brew services start epheterson/immich-accelerator/immich-accelerator
```

This uses the formula's own service definition, survives `brew upgrade`, and restarts at login. Check it with `brew services list`. Stop with `brew services stop epheterson/immich-accelerator/immich-accelerator`.

> Don't also install the launchd plist below if you're using `brew services`. Running both double-starts the watcher.

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
| **ffmpeg encoders** | Software H.264/HEVC | VideoToolbox hardware H.264/HEVC via wrapper | Hardware-encoded output has slightly different bitstream characteristics. Visually equivalent. A lightweight wrapper remaps Immich's software encoder requests to VideoToolbox hardware equivalents. Immich has no VideoToolbox option, so it logs `Transcoding video ... without hardware acceleration` even though the encode runs on the GPU via the wrapper. That log is expected and benign. |
| **Sharp / libvips** | Prebuilt linux-arm64 Sharp | Rebuilt against Homebrew system libvips | Identical image output. System libvips handles corrupt HEIF files more gracefully (matches Docker's error handling). |
| **ML: CLIP** | ONNX Runtime | Native Swift: mlx (default ViT-B-32, plus the full SigLIP/SigLIP2 family) or onnxruntime (any other model, using Immich's own ONNX exports) | Same models and weights, and the mlx path computes in bfloat16 with layer-norm statistics and the final normalize kept in fp32. Text embeddings match Docker closely (cosine 0.9998+); image embeddings are numerically close (>0.999 cosine against Immich's own ONNX export, spot-checked on real library previews for `ViT-B-16-SigLIP-256`, `ViT-L-16-SigLIP2-256` and `ViT-SO400M-16-SigLIP2-384` rather than all 17; ~0.999 for onnxruntime-path models, resize-filter floating point). Search results are equivalent and indexes built on this engine stay valid across upgrades. **One exception, in 1.10.0 only:** if you use a SigLIP, SigLIP2 or `dfn5b` model, embeddings created *before* 1.10.0 used the wrong crop and you should re-run Smart Search once. |
| **ML: Face detection** | ONNX Runtime (antelopev2/buffalo detector) | Apple Vision framework (Neural Engine) | Different detector. Accuracy is comparable; bounding boxes may differ slightly. |
| **ML: Face recognition** | ONNX Runtime | Native Swift: same InsightFace ArcFace model via onnxruntime (CPU) | Same model and weights; embeddings match Docker (~0.9997, image-decoder floating point). Existing face clusters stay valid. |
| **ML: OCR** | PaddleOCR via ONNX | Apple Vision framework (Neural Engine) | Different engine. Vision framework OCR is generally more accurate for Latin text, may differ for CJK. |
| **HEIC decode** | libvips built with libde265 | Homebrew `vips` (libvips + libde265), then Sharp | Sharp's prebuilt libvips on macOS has no HEVC decoder, so iPhone HEICs are pre-decoded by the Homebrew `vips` (the same libvips + libde265 Docker uses) before Sharp processes them. Works headless; pixels match Docker. Apple ImageIO (`sips`) is a last-resort fallback for a logged-in desktop only. |
| **Camera RAW decode** | libvips with fuller libtiff/libjpeg + libraw | Homebrew `vips`, then Sharp | Sharp's prebuilt libvips on macOS lacks old-style-JPEG and dcraw/libraw support, so Canon CR2/CR3, Nikon NEF, Sony ARW, Adobe DNG and other RAW originals fail thumbnail generation (`tiff2vips: Old-style JPEG compression support is not configured`, or a `multiband -> srgb` colourspace error). They are pre-decoded by the Homebrew `vips` (fuller libtiff/libjpeg for TIFF-based RAW, plus libraw for the rest), the same libvips Docker uses, before Sharp. Works headless; matches Docker. |

### What this means in practice

- **Thumbnails, previews, and video**: Identical to Docker. Same jellyfin-ffmpeg binary, same `tonemapx` HDR tone mapping, same output. VideoToolbox hardware encoding is faster but visually equivalent.
- **HEIC photos**: Thumbnails generate correctly. The default iPhone format (HEVC-coded HEIC, often tiled) is decoded by the Homebrew `vips` (libvips + libde265, a formula dependency) since Sharp's bundled libheif is AVIF-only. This works with no logged-in GUI session (a headless Mac Mini), and output matches Docker. Apple ImageIO (`sips`) remains a fallback but needs a GUI session, so it isn't relied on.
- **Camera RAW photos** (Canon CR2/CR3, Nikon NEF, Sony ARW, Adobe DNG, and other RAW formats): Thumbnails generate correctly. Sharp's bundled libvips on macOS can't decode these (no old-style-JPEG support, no dcraw/libraw), so they're pre-decoded by the Homebrew `vips` (fuller libtiff/libjpeg plus libraw), the same libvips Docker uses. Works headless.
- **CLIP search**: Search results are equivalent but not identical. A search that returns 20 results in Docker will return ~18-20 of the same results natively, possibly in slightly different order.
- **Face grouping**: Faces are detected and grouped correctly. The grouping boundaries may differ slightly (e.g., a borderline face might be grouped differently).
- **OCR**: Text extraction is at least as good as Docker for English/Latin text.

## Troubleshooting

The accelerator will tell you what's wrong. Click a symptom below for the fix.

<details>
<summary><b>Setup says "Upload: not detected"</b></summary>

Symptom: `immich-accelerator setup` finds your Immich container but reports `Upload: not detected`.

Cause: fixed in v1.5.8. Older versions only recognized uploads mounted under a `/upload` path; the modern Immich compose mounts `${UPLOAD_LOCATION}:/data` and leaves `IMMICH_MEDIA_LOCATION` unset, so detection missed it.

Fix: `brew upgrade immich-accelerator` and re-run setup. If you're on a same-machine Docker Desktop setup where the container path (`/data`) differs from the host mount, the absolute paths still have to match for the native worker to read them (see [Split deployment](docs/split-deployment.md)); the simplest fix is to set `IMMICH_MEDIA_LOCATION` (and the bind mount) to the host path so both sides agree.

</details>

<details>
<summary><b>Thumbnails 404 in the Immich web UI</b></summary>

Symptom: the native worker runs happily, but Immich's API server logs `ENOENT: /data/thumbs/.../xxx_thumbnail.webp` and thumbnails never show up.

Cause: split-setup path mismatch. Docker Immich stores absolute paths like `/data/library/<uuid>/...` in Postgres; the native worker writes to your `upload_mount` which is something else. Docker API then 404s the stored path.

Fix: run `immich-accelerator setup --url http://your-nas:2283 --api-key YOUR_KEY` again. v1.4.1+ detects Docker's media root via the API and refuses to save a broken config. You'll see the mismatch explicitly with both walkthroughs (match Docker, or synthetic link on Mac). See [Split deployment](docs/split-deployment.md) for the two options.

</details>

<details>
<summary><b>Microservices red after editing <code>/etc/synthetic.d/immich-accelerator</code> by hand</b></summary>

Symptom: you added your own line to `/etc/synthetic.d/immich-accelerator` (e.g. a split-deployment upload path), rebooted, and Microservices is red. The native worker won't start because `/build` doesn't resolve.

Cause: that file also holds the required `/build` synthetic link (for Immich 2.7+ plugin paths). Before v1.5.7, setup treated the file *existing* as "build link configured" and skipped writing the entry, so a hand-edited file silently lost `/build`.

Fix: upgrade to v1.5.7+ and re-run setup; it now checks for the actual `build` entry and appends it without touching your other lines. Or add it yourself and reboot:

```bash
# /etc/synthetic.d/immich-accelerator (needs a build entry, tab-separated)
printf 'build\t%s\n' "${HOME#/}/.immich-accelerator/build-data" | sudo tee -a /etc/synthetic.d/immich-accelerator
```

</details>

<details>
<summary><b>ML jobs fail with "Machine learning request failed for all URLs"</b></summary>

Symptom: Immich's worker log shows ML requests failing with HTTP 500 on every URL, even though `immich-accelerator status` says the ML service is running.

Diagnose: run:

```bash
immich-accelerator ml-test
```

This exercises `/ping`, `/health`, CLIP visual, and OCR with a synthetic image. On any failure it tails the last 30 lines of `~/.immich-accelerator/logs/ml.log` and prints the three most common root-cause fixes. Paste the output in a GitHub issue if you're stuck.

Common causes:

- **Partial HuggingFace model cache**: `rm -rf ~/.cache/huggingface/hub/models--mlx-community--clip-vit-base-patch32` then `immich-accelerator start`
- **mlx / mlx-clip version mismatch**: `brew reinstall immich-accelerator`
- **Stale model files**: `rm -rf ~/.immich-accelerator/ml/models` then restart

</details>

<details>
<summary><b>Dashboard crashes with <code>ModuleNotFoundError: No module named 'uvicorn'</code></b></summary>

Fixed in v1.4.1. If you're on an older release, `brew upgrade immich-accelerator` and re-run. The formula wrapper now runs the CLI under the ML venv's Python, which has fastapi + uvicorn installed.

</details>

<details>
<summary><b><code>immich-accelerator setup</code> fails with <code>ENOENT: /build/corePlugin/manifest.json</code></b></summary>

Fixed in v1.4.1. The OCI image extractor used to skip small layers that contained the Immich 2.7+ `corePlugin` WASM files. Upgrade and re-run setup.

</details>

<details>
<summary><b><code>brew install</code> fails with "Refusing to load formula ... from untrusted tap"</b></summary>

Homebrew 5.1.15 (June 2026) requires third-party taps to be explicitly trusted before it will load their formulas. The fix is one command:

```bash
brew trust epheterson/immich-accelerator
```

Using the fully-qualified name (`brew install epheterson/immich-accelerator/immich-accelerator`, as in the quick start) bypasses the check for that one command (Homebrew treats naming the tap explicitly as consent), but `brew upgrade` still skips the tap until it's trusted.

</details>

<details>
<summary><b><code>brew upgrade</code> never finds a new version</b></summary>

Symptom: `brew upgrade immich-accelerator` reports nothing to do (and `brew outdated` shows nothing), but GitHub has a newer release. `brew info immich-accelerator` shows the real error: `Refusing to load formula ... from untrusted tap`.

Cause: the same trust requirement as above, but for taps added *before* Homebrew 5.1.15 there's no error: Homebrew *silently skips* untrusted formulas during `outdated`/`upgrade`, so your install goes stale with no warning.

Fix:

```bash
brew trust epheterson/immich-accelerator
brew update && brew upgrade immich-accelerator
immich-accelerator stop && immich-accelerator start
```

</details>

## Security

- Config file (`~/.immich-accelerator/config.json`) is chmod 600
- Postgres exposed on `127.0.0.1:5432` (localhost only) by default
- Redis exposed on `127.0.0.1:6379` (localhost only) by default
- Dashboard binds on `0.0.0.0:8420` (LAN-accessible). The Re-queue button triggers job processing via the Immich API. If you're on an untrusted network, don't run the dashboard or bind to localhost only

## On agentic engineering

This project was built iteratively across several sessions with [Claude Code](https://claude.com/claude-code) (Opus 4.6). From zero knowledge of the Immich codebase to a working native accelerator, including upstream contributions to the ML service and a feature discussion with the Immich maintainers. Inspect the code yourself, use it and share it, or don't.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=epheterson/immich-apple-silicon&type=Date)](https://star-history.com/#epheterson/immich-apple-silicon&Date)

---

## License

MIT

## Credits

Built on [Immich](https://immich.app/) · [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal) · [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg) · [Sharp](https://sharp.pixelplumbing.com/)

Two projects we learned from: the menu-bar app's design is inspired by [Immich-Accelerator-Helper](https://github.com/pl4za/Immich-Accelerator-Helper) by [@pl4za](https://github.com/pl4za), and running Immich's own ONNX model exports natively via onnxruntime was informed by [michina-swift](https://github.com/lucka-me/michina-swift) by [@lucka-me](https://github.com/lucka-me).

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.com/claude-code).
