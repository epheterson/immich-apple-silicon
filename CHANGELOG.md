# Changelog

## 1.4.3 — 2026-04-14

### Fixes
- **NODE_OPTIONS shim path broken in v1.4.2 (#24 follow-up)**: The v1.4.2 commit that "polished" the pg_dump shim wrapping wrapped the path in single quotes, thinking Node would unquote them. Node doesn't — it splits `NODE_OPTIONS` on whitespace and takes the literal characters. The quotes became part of the filename and every worker start failed with `Cannot find module ''/opt/homebrew/Cellar/…/pg_dump_shim.js''`. Fix: backslash-escape spaces (which Node does honor) instead of quoting. Verified against a real v1.4.2 brew install on an M4 Mac — rewrite works, worker boots, no ENOENT.
- **ORJSONResponse crash in ML service (#20 tail)**: The v1.3.4 commit dropped `orjson` from `ml/requirements.txt` with an incorrect commit message claiming "not imported by our code." `main.py` imports `ORJSONResponse` from `fastapi.responses` and uses it in 3 places, and FastAPI's `ORJSONResponse.render()` asserts `orjson is not None` at render time — every `/predict` request crashed with `AssertionError: orjson must be installed to use ORJSONResponse`. Fix: swap to stdlib-backed `JSONResponse`, keeping the `orjson` drop (which was correct — its wheel broke Homebrew's dylib fixup). Verified end-to-end: ml-test passes 4/4 on real CLIP + OCR requests with no orjson in the venv.

### Regression guards (test coverage the v1.4.2 VM E2E didn't have)
- Static check: `ml/src` uses `ORJSONResponse` only if `ml/requirements.txt` pins `orjson` — would have caught the v1.3.4 half-fix instantly.
- Real-node integration: generate the exact `NODE_OPTIONS` string `cmd_start` builds, spawn `node --require` against a sentinel shim, assert it loads. Would have caught the v1.4.2 quoting bug before merge.
- Static check: `cmd_start`'s shim-path escaping uses backslash form, not shell quoting.

## 1.4.2 — 2026-04-13

### Fixes
- **Path-mismatch probe false positive (#19 follow-up)**: The v1.4.1 split-setup probe queried `/api/libraries` as its primary signal, but that endpoint only returns **external** libraries in Immich 2.7+ — the upload library (where web-UI uploads land) is implicit at `IMMICH_MEDIA_LOCATION` and doesn't appear there. Result: any install with an external library plus a correctly-set `upload_mount` got blocked at `immich-accelerator start` with a false "path mismatch" error. The probe now parses an upload-library asset's `originalPath` (filtering `libraryId: null`) and skips external-library assets entirely. If no upload assets exist yet, the check is skipped — nothing to compare against.
- **External library path validation**: The probe now also checks every external library's `importPath` against the Mac filesystem. Missing paths produce a non-fatal warning per library with the library name and guidance to mount or synthetic-link them. Upload-library mismatch remains fatal (thumbnails WILL 404), external-library inaccessibility is advisory (worker can still process uploads + any libraries whose paths do resolve).
- **pg_dump ENOENT on database backup (#24)**: Immich's `DatabaseBackupService` hardcodes `/usr/lib/postgresql/${version}/bin/pg_dump` in its dist, and on macOS that path doesn't exist, so the native microservices worker fails every backup cycle with ENOENT. `/usr/lib/` is SIP-protected and there's no env-var escape hatch in Immich's code. Fix: a tiny Node runtime shim (`immich_accelerator/hooks/pg_dump_shim.js`) that monkey-patches `child_process.spawn`/`spawnSync`/`execFile` to rewrite the Linux postgres client path to `/opt/homebrew/opt/libpq/bin/` at call time. The shim is preloaded via `NODE_OPTIONS=--require …` when we launch the worker. **Immich's JS source on disk is never touched** — the same interposition pattern we already use for the ffmpeg wrapper, applied at the Node module layer. Verified end-to-end on a real Mac with node + libpq 18.3.

### Docs
- **Split deployment clarity**: Reworked the Split deployment section of the README to lead with the one requirement everyone needs to get right — both machines see the same files at the same absolute paths via a shared filesystem (NFS/SMB). There is no HTTP transport of thumbnails between hosts; the worker reads/writes directly to disk. Surfaced because a v1.4.1 user interpreted "match paths" as string matching. Also dropped the long-stale v0.x and v1.2.x migration sections.

### Test infrastructure
- **VM bootstrap script**: Fixed three edge cases that kept the tart-based E2E harness from running cleanly — trap composition bug that left stale VMs behind, stdin-race on heredoc-over-ssh that silently dropped pip install commands, and `brew update` network flakiness aborting bootstrap. The harness is now proven end-to-end against live Immich.

## 1.4.1 — 2026-04-12

### Fresh-install fixes
- **Dashboard ModuleNotFoundError** (#17): Homebrew formula wrapper now runs the CLI under the ML venv's Python instead of stock `python@3.11`. The dashboard's lazy `fastapi`/`uvicorn` imports now resolve on any fresh Mac — previously they only worked if the user happened to have the packages installed globally. Also added a `brew test` block that force-loads `dashboard.create_app` so this class of bug gets caught at audit time, not in the wild.
- **Missing corePlugin on OCI download** (#18): Re-fix of a regression from the v1.3.3 fix. The layer loop had a `size_mb < 1` early-break shortcut that fired *before* examining the current layer — stranding the tiny corePlugin COPY layer. Replaced with a version-aware break (`_has_everything`) that requires `corePlugin/manifest.json` for Immich 2.7+. Verified end-to-end against a live ghcr.io pull.

### Test coverage
- Added `tests/test_fresh_install.py` — unit tests for the layer-break logic, a static check that `fastapi`/`uvicorn` stay pinned in `ml/requirements.txt`, an AST check that `dashboard.py` has no top-level third-party imports, and a slow integration test that builds a pristine venv and confirms `create_app` works with only `fastapi`+`uvicorn` installed.
- **CI now runs on macOS 14** (Apple Silicon) in addition to Ubuntu. The `fresh-install-macos` job catches "works on my machine" bugs before shipping.

## 1.4.0 — 2026-04-09

- **libpq as Homebrew dependency**: `psql` now installed automatically via `depends_on "libpq"` in the formula — works for both new installs and upgrades, not just setup.
- **Published GitHub releases**: Releases are now published automatically (not draft).

## 1.3.9 — 2026-04-09

- **Draft GitHub releases**: Merging to main now creates a draft GitHub release with changelog notes and upgrade instructions. Review and publish when ready.

## 1.3.8 — 2026-04-09

- **Dashboard DB connectivity**: Dashboard now logs clear errors when it can't reach Postgres (missing psql, wrong port binding, unreachable host) instead of silently showing empty data.
- **Setup installs libpq**: `psql` client installed automatically for dashboard DB queries. Previously only worked on same-machine setups via docker exec.
- **Port instructions**: Setup now suggests open port binding (`5432:5432`) instead of localhost-only (`127.0.0.1:5432:5432`), with a note to restrict if same-machine.

## 1.3.7 — 2026-04-08

- **Fully automated releases**: Merging to main now auto-tags and updates Homebrew formula — no manual steps.

## 1.3.6 — 2026-04-08

- **Homebrew formula fix**: Move ML pip install to `post_install` phase — fixes dylib fixup errors on all Rust-compiled Python extensions (pydantic_core, tokenizers, etc.), not just orjson.
- **CI workflow**: Formula template now generates `post_install` correctly so future releases don't regress.
- **Docs**: Clarify NAS+Mac path mapping with two concrete options — match Mac paths in Docker, or use macOS synthetic links to match Docker paths on Mac (zero Docker changes).

## 1.3.5 — 2026-04-08

### Bug fixes
- **jellyfin-ffmpeg auto-detect**: No longer hardcodes a specific version URL that 404s when upstream updates. Now parses the repo directory listing for the latest build.
- **Homebrew formula**: Move ML venv pip install to `post_install` phase to avoid Homebrew's dylib fixup errors on Rust-compiled Python extensions.
- **Cleanup**: Remove stale local formula copy (real formula lives in tap repo, auto-updated by CI).

## 1.3.4 — 2026-04-08

### Bug fixes
- **Homebrew install fix**: Drop `orjson` dependency — binary wheel broke Homebrew's dylib fixup. Not imported by our code; FastAPI uses stdlib json as fallback. Fixes #7.
- **Synthetic link migration**: Migrate legacy `/etc/synthetic.conf` entry to `/etc/synthetic.d/immich-accelerator`. Uninstall now cleans both locations.

## 1.3.3 — 2026-04-07

### Bug fixes
- **Plugin WASM paths**: Fix Immich 2.7+ crash in split-worker setups. Uses macOS synthetic firmlink (`/build` → `~/.immich-accelerator/build-data`) so both Docker and native workers resolve the same plugin paths. No database modifications. Setup prompts for sudo once; `uninstall` cleans it up.
- **OCI extraction**: Fix build data landing in wrong directory (`build/` instead of `build-data/`). Tar member paths are now rewritten during extraction.
- **Missing corePlugin**: OCI download no longer skips small image layers, so the corePlugin WASM (in its own Docker COPY layer) is always extracted.

## 1.3.2 — 2026-04-07

- Yanked — used direct DB manipulation for plugin path fix. Replaced by v1.3.3 firmlink approach.

## 1.3.1 — 2026-04-05

- **Homebrew install**: `brew tap epheterson/immich-accelerator && brew install immich-accelerator`. Installs deps, creates `immich-accelerator` command in PATH, supports `brew services start` for auto-launch.
- Formula at [epheterson/homebrew-immich-accelerator](https://github.com/epheterson/homebrew-immich-accelerator).

## 1.3.0 — 2026-04-04

### Module renamed
- `python3 -m immich_accelerator` (was `python3 -m accelerator`). Clearer what it is.

### One-command setup
- `git clone --recursive && cd && python3 -m immich_accelerator setup` does everything.
- Auto-installs: Homebrew, Node.js, libvips, Python 3.11, ML venv + dependencies, jellyfin-ffmpeg.
- Extracts server from Docker, rebuilds Sharp for macOS.
- Shows docker-compose changes, opens editor, retries connection until Docker is configured.
- Auto-starts worker + ML service after setup.
- Offers to install launchd services (worker + dashboard) for auto-start on login.
- Quick start reduced from 4 manual steps to: clone, setup, done.
- `uninstall` command: cleanly removes services, launchd config, accelerator data, and ML venv. Immich data and Docker untouched.

## 1.2.2 — 2026-04-04

- Setup offers to install Homebrew, Node.js, and libvips if missing. Zero manual prerequisites beyond Python 3.11.
- OrbStack Docker path detected automatically.

## 1.2.1 — 2026-04-04

- **jellyfin-ffmpeg**: Setup now downloads the same ffmpeg binary Immich uses in Docker (jellyfin-ffmpeg, macOS arm64). Includes `tonemapx` natively — HDR video thumbnails are now identical to Docker output. No more Homebrew ffmpeg patching or formula editing.
- **Simplified ffmpeg wrapper**: With jellyfin-ffmpeg handling `tonemapx` natively, the wrapper only remaps encoders to VideoToolbox. Reduced from 120 lines to 50.
- **Requirements simplified**: No longer need `brew install ffmpeg` or libwebp formula patching. Just Node.js, libvips, and Python 3.11+.
- **Known differences reduced**: ffmpeg row in the differences table now shows "Identical" — same binary, same filters, same output.

## 1.2.0 — 2026-04-04

### Dashboard
- Chip-as-card layout: each card represents a silicon unit (CPU, GPU, Neural Engine, VideoToolbox) with its tasks inside. Neural Engine shows Face Detection and OCR with individual progress bars and counts.
- Excludes hidden assets (Live Photo motion files) from progress — matches Immich's actual processing scope.
- Shows 100% when all processable assets are complete. Unprocessable files (corrupt, stubs) tracked as "skipped" instead of dragging percentage below 100%.
- Video Transcode shows "N transcoded" instead of misleading ratio against total videos.

### Housekeeping
- PLAN.md removed from repo (local planning doc, gitignored)
- Pre-push hook enforces VERSION > remote + CHANGELOG entry

## 1.1.0 — 2026-04-03

### Setup: remote and manual modes
- **Remote Immich** (`setup --url http://nas:2283 --api-key KEY`) — queries the Immich API for version, prompts for DB/Redis connection details. Docker on the Mac is optional — if available it pulls the image; if not, it guides you through extracting the server on your NAS and importing with `--import-server`.
- **Manual config** (`setup --manual`) — creates a config template at `~/.immich-accelerator/config.json` for direct editing. Full control, zero auto-discovery.
- **Server import** (`setup --import-server PATH`) — import server files from a directory or tarball extracted on a remote host. No Docker needed on the Mac.

### Web dashboard
- Real-time monitoring at `http://your-mac:8420` with live processing rates and ETAs
- Service health indicators (worker, ML, Docker)
- Re-queue Missing button (triggers all job queues via Immich API)
- Apple Silicon hardware utilization display
- Mobile-friendly layout

### FFmpeg fixes
- **libwebp encoder validation** — setup now checks for `libwebp`, `h264_videotoolbox`, and `hevc_videotoolbox` encoders. Without libwebp, all video thumbnails fail silently.
- **tonemapx → tonemap remap** — Immich's Docker ffmpeg (jellyfin-ffmpeg) has a custom `tonemapx` filter for HDR→SDR. Homebrew ffmpeg doesn't. The wrapper now remaps to upstream `tonemap` + `format` filters for HDR video thumbnails.
- **Two-pass -preset handling** — `-preset` is now correctly stripped for VideoToolbox regardless of argument order.
- **Debug logging** — ffmpeg wrapper logs rewritten commands to `~/.immich-accelerator/logs/ffmpeg-wrapper.log`.

### Fixes
- Sharp rebuilt against system libvips (Homebrew) instead of prebuilt darwin binaries. Handles corrupt HEIF files correctly, matching Docker's behavior.
- Stale process cleanup on `start` — kills orphaned immich/ML processes from previous runs.
- Dashboard uses config values for DB queries instead of hardcoding container/user/db names.
- `cmd_start` always uses config DB password (not stale Docker detection).
- `cmd_watch` uses `extract_immich_server` return value instead of reconstructing path.
- Rate calculations use `time.monotonic()` consistently (immune to NTP/DST clock adjustments).
- API key preserved across `setup` re-runs.
- Submodule pointer fixed (clone --recursive works).

### Documentation
- "Known differences from Docker" section in README — comprehensive table of every deviation from stock Immich.
- Split deployment (NAS + Mac) documentation updated for new setup modes.
- Dashboard section with screenshot.

### Both versions visible
- Dashboard header shows both "Accelerator v1.1.0" and "Immich v2.6.3" badges.
- VERSION file is single source of truth, read by both CLI and dashboard.

## 1.0.0 — 2026-04-01

Major rewrite. The project is now **Immich Accelerator** — runs Immich's own microservices worker natively on macOS instead of custom thumbnail/ffmpeg components.

### What's new
- **Native microservices worker** — Immich's own code extracted from Docker, run bare metal on macOS with full hardware access
- **VideoToolbox video transcoding** — ffmpeg wrapper remaps software encoders to hardware (h264_videotoolbox, hevc_videotoolbox). No Immich patches needed.
- **Accelerator CLI** — `setup`, `start`, `stop`, `status`, `watch`, `update`, `logs`
- **Container extract approach** — server copied from Docker image, always version-matched. Sharp native binary + libvips for HEIF swapped for macOS. No source builds.
- **IMMICH_MEDIA_LOCATION** — Immich's official env var for path mapping. Zero sudo.
- **Auto-update** — detects Immich version changes on start and re-extracts
- **Watchdog** — `watch` command monitors and auto-restarts crashed services
- **Metal concurrency** — shared gpu_lock serializes MLX CLIP (thread-safety bug in MLX), Vision framework runs lock-free on ANE
- **HEIF/HEIC support** — Sharp libvips darwin binary includes full format support

### Hardware utilization (M4 24GB)
| Silicon | Used for | Rate |
|---------|----------|------|
| Metal GPU | CLIP embeddings (MLX) | ~700/min |
| Neural Engine | Face detection + OCR (Vision) | ~200 + 700/min |
| VideoToolbox | Video encode/decode | Hardware-accelerated |
| CPU (NEON SIMD) | Thumbnails (Sharp/libvips) | ~400/min |
| CPU / CoreML | Face embedding (ONNX) | Lock-free, parallel |

### What's removed
- Custom thumbnail worker (replaced by Immich's own Sharp running natively)
- FFmpeg proxy and wrapper scripts (replaced by lightweight ffmpeg wrapper)
- Direct database writes for thumbnails (native worker uses Immich's own job pipeline)

### What's unchanged
- ML service (immich-ml-metal) — native ML via MACHINE_LEARNING_URL

### ML service updates
- Concurrent inference: CLIP, face detection, OCR run simultaneously across GPU/ANE/CPU
- Batched face embeddings: single ONNX call for N faces
- In-memory CLIP: eliminated temp file I/O
- Metal concurrency fix: shared gpu_lock prevents MLX + Vision crashes
- 29 tests

## 0.1.0–0.1.8 — 2026-03-23 to 2026-03-30

Initial release and iterations. Custom thumbnail worker, ffmpeg proxy, ML service. See git history for details.
