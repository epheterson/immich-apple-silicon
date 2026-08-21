# Using the accelerator

Day-to-day operation once you're set up — see the [quick start](../README.md#quick-start) if you haven't installed yet.

## Commands

Every command is prefixed with `immich-accelerator` (e.g. `immich-accelerator setup`).

| Command | What it does |
|---------|-------------|
| `setup` | Auto-detect local Docker, extract server, configure |
| `setup --url URL` | Set up from a remote Immich instance |
| `setup --manual` | Create a config template for manual editing |
| `setup --ml-only` | Set up this Mac as an [ML-only network compute node](deployment.md#ml-only-network-node) |
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

If a command isn't behaving as documented here, see [troubleshooting.md](troubleshooting.md).

## Running as a service (recommended)

Run the accelerator as a background service, not with a bare `immich-accelerator start`. The service runs `watch` mode, and **only `watch` mode gives you**:

- **Auto-restart**: launchd (`KeepAlive`) restarts the monitor if it dies; the monitor restarts the worker, ML, and dashboard if they crash.
- **Auto-update**: picks up new Immich versions (re-extracts the worker) and notifies on accelerator updates — see [Updates](#updates) below.
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

## Updates

The accelerator handles Immich updates automatically:

- **On every `start`:** checks the Docker container version, re-extracts if it changed
- **In `watch` mode:** checks every 5 minutes. If Watchtower or a manual `docker compose pull` updates Immich, the watchdog stops the worker, re-extracts the new server, and restarts. No manual intervention needed.
- **Manual:** `immich-accelerator update` if you prefer to control the timing

To update the accelerator itself:

```bash
brew upgrade immich-accelerator
```

If you run it as a service (`watch` mode, the recommended setup above), that's all you need: within ~30s the watcher notices the new version on disk, relaunches itself, and restarts the worker and ML service on the new code. (A detached worker survives a plain restart, so this version-aware reload is what guarantees the new code actually takes effect; `brew services restart` alone wouldn't reload the worker.)

If you run the worker manually instead of as a service, restart it yourself after upgrading:

```bash
immich-accelerator stop && immich-accelerator start
```

If `brew upgrade` says there's nothing to do but you know a newer release exists, see [Troubleshooting](troubleshooting.md#brew-upgrade-never-finds-a-new-version).

## Dashboard and menu bar app

### Dashboard

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

![Dashboard](dashboard.png)

The dashboard binds `0.0.0.0:8420` (LAN-accessible) by default — see [security.md](security.md) before exposing it beyond a trusted network.

### Menu bar app

A native menu-bar app shows accelerator health at a glance and covers the daily actions: worker / ML / dashboard status (with a NATIVE or PYTHON engine badge), start / stop / restart, run `ml-test` inline, open Immich, the dashboard, or logs, and launch-at-login. It reads the accelerator's own state directly (no extra services) and weighs about 90KB.

```bash
brew install --cask epheterson/immich-accelerator/immich-accelerator-menubar
open "/Applications/Immich Accelerator.app"
```

Design inspired by [Immich-Accelerator-Helper](https://github.com/pl4za/Immich-Accelerator-Helper) by [@pl4za](https://github.com/pl4za).

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

**This goes as far as the process boundaries and no further.** Video, thumbnails, metadata and RAW decode all happen inside the single Immich microservices worker, so "video but not thumbnails" isn't something the accelerator can offer. That split is Immich's job scheduler: use **Administration → Jobs** to pause individual queues, and see [Performance tuning](#performance-tuning) below for concurrency.

`worker off, ml on` is common enough to have its own setup flag — see [ML-only network node](deployment.md#ml-only-network-node).

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

## Configuration details

<<<<<<< HEAD
### Setting `IMMICH_ACCEL*` variables

Put them in the `env` block of `~/.immich-accelerator/config.json`:

```json
{
  "env": {
    "IMMICH_ACCELERATOR_HEIC_DECODE_CONCURRENCY": "2",
    "IMMICH_ACCEL_ML_THREADS": "4"
  }
}
```

They are passed to the worker and the ML service, and the accelerator reads its own from there too. Restart with `brew services restart immich-accelerator` to apply.

A real environment variable still wins, so running the accelerator by hand with one exported does what you would expect. The config block exists because the environment cannot be reached on a Homebrew install, not to outrank it.

Setting them in the shell environment does not work on a Homebrew install, which is why this exists: `brew services` generates the launch agent, it carries no environment, `launchctl setenv` does not reach it, and editing the plist is undone the next time the service restarts.

Only `IMMICH_ACCEL*` names are accepted. Everything else a service needs is worked out at startup, and anything else in that block is ignored with a warning.
=======
### How this Mac differs from Docker

One setting decides it, in Settings under Processing, or from the CLI:

```bash
immich-accelerator encoding preset          # what is set now
immich-accelerator encoding preset stock    # change it
```

| Position | What it means |
|---|---|
| **Stock** | Every output identical to Docker: video, thumbnails, faces and text. Nothing runs on the video hardware, and machine learning runs Immich's own ONNX models rather than Apple's frameworks. A library built here can move back to a Docker install without reprocessing. |
| **Balanced** | The default. Video encoded and decoded on the hardware, machine learning on Apple Silicon. Thumbnails from 10-bit video are visually identical to Docker's but not byte-identical. |
| **Maximum** | Hardware wherever it measured faster, which leaves the most CPU for other jobs. Audio is re-encoded with AudioToolbox, so those files differ from Docker's. |

Each position sets the individual switches below, and they stay visible: change one by hand and the position reads `custom`, which is a real answer rather than an error.

**Stock uses the Python machine learning engine**, because Immich's own models are what make the output match and only that engine carries them. Switching between Stock and the others therefore changes the face detector, and the two detectors do not agree on exactly where faces are. Faces already in your library keep the boxes they were found with; re-run Face Detection in Immich if you want them redone.

### Hardware video encoding

On by default. Set `IMMICH_ACCEL_HW_VIDEO=0` to keep Immich's own software encoder instead.

Worth knowing what the switch trades, because "hardware" does not simply mean "faster". Immich asks ffmpeg for `preset ultrafast`, which is genuinely quick, so on an idle Mac the software encoder often finishes a single file sooner. What VideoToolbox buys is the rest of the machine. Measured on an M4 over 20 seconds of 1080p camera footage: software finished in 1.5 seconds but spent 12.5 seconds of CPU across about eight cores, while VideoToolbox took 2.8 seconds of wall clock and 5 seconds of CPU across about two. On a Mac also running Immich's other jobs and the machine learning engine, that is usually the trade you want.

Quality is not the thing you give up, and on real footage it often goes the other way. `preset ultrafast` disables most of what x264 is good at, which shows up badly on grainy or high-motion video: on that same footage the software encode scored SSIM 0.967 against the original while the hardware encode scored 0.974 in half the file size. On synthetic test patterns the ranking reverses, which is exactly why the numbers below come from a command you run on your own files rather than from a table here.

`immich-accelerator encode-compare <video>` runs both on a file of yours and prints the numbers, including which hardware quality setting lands closest to what Immich would have produced on its own.
>>>>>>> feat/encoder-toggles

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
