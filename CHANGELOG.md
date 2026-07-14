# Changelog

## 1.5.30 - 2026-07-14

### Fixed
- **Camera RAW thumbnails failed** (#99): Canon CR2 (and other camera RAW such as Nikon NEF, Sony ARW, Adobe DNG, Fujifilm RAF) died in `AssetGenerateThumbnails` with `tiff2vips: Old-style JPEG compression support is not configured`. Sharp's prebuilt libvips on macOS lacks old-style-JPEG support and has no dcraw/libraw loader, so it cannot read a RAW file's embedded image. The HEIC decode shim now also detects RAW by extension and pre-decodes it via Homebrew `vips` (fuller libtiff/libjpeg for TIFF-based RAW like CR2/NEF/ARW/DNG, plus libraw for the rest), the same libvips Docker Immich uses, before handing the result to Sharp. Works headless; OCR and face are untouched (RAW decode affects thumbnail generation only). Verified on an M4 Mac Mini with a real Canon CR2: bundled Sharp alone fails with the tiff2vips error, and with the shim it produces a valid thumbnail. Closes another macOS-only decode gap after the HEIC fix (v1.5.28). Reported by [@shtefko](https://github.com/shtefko).
- **Headless HEIC decode was silently falling back to `sips`**: Sharp's prebuilt libvips exports a bogus `VIPSHOME` (the GitHub-runner path it was built in, e.g. `/Users/runner/work/sharp-libvips/...`) into the process environment when it loads. The decode shim spawned the Homebrew `vips` with that inherited `VIPSHOME`, so `vips` looked for its loader modules (libheif for HEIC, the RAW loaders) in a nonexistent directory and failed with `VipsForeignLoad: ... is not a known file format`, falling back to Apple's `sips`. Because `sips` needs a logged-in GUI session, on a headless Mac the primary `vips` path (the entire point of the v1.5.28 HEIC fix) never actually ran, so HEIC thumbnails quietly depended on a desktop session. The shim now drops a `VIPSHOME` that points at a nonexistent directory before spawning a decoder, so `vips` uses its own compiled-in module path and decodes HEIC and RAW headless as intended; an intentional, existing `VIPSHOME` is preserved. Surfaced while adding RAW support and verified on a headless M4 Mac Mini (HEIC now decodes via `vips`, not `sips`).

### Performance
- The `vips` decode intermediate is now written as a lossless deflate-compressed TIFF (roughly 40% smaller than uncompressed) to trim peak memory and temp I/O when decoding large RAW frames. Pixels are unchanged; Sharp reads the deflate TIFF the same way.

## 1.5.29 - 2026-07-14

### Changed
- **Lifted the mlx pin now that 0.31.2's CLIP crash is fixed** (#38): the bundled `immich-ml-metal` fork pinned `mlx>=0.22.0,<0.31.2` because mlx 0.31.2 hard-crashed CLIP inference in the worker's thread pool (`std::runtime_error: There is no Stream(gpu, 0) in current thread`, SIGABRT). mlx 0.32.0 is verified to fix it: 1,800 concurrent CLIP inferences from a 4/8-worker `ThreadPoolExecutor` against real `clip-vit-base-patch32` weights on Apple Silicon, zero aborts. The pin is now `mlx>=0.22.0,!=0.31.2,<0.33.0`, which excludes only the known-bad 0.31.2, allows the verified 0.32.x (currently the sole in-range build), and caps below the untested 0.33 minor. The weekly mlx-pin check now tracks that 0.33.0 ceiling instead of 0.31.2, so it stops nagging about 0.32.0. Blast radius is CLIP only: OCR runs on Apple Vision, face on onnxruntime, neither imports mlx.

## 1.5.28 — 2026-07-12

### Fixed
- **HEIC thumbnails failed on headless Macs** (`Input file contains unsupported image format` on `AssetGenerateThumbnails`): the HEIC decode shim transcoded via Apple's `sips`, but `sips` needs a logged-in GUI (Aqua/WindowServer) session, so in a headless launchd service it silently produced empty output and no HEIC thumbnail was generated. The shim now decodes via libheif (`vips`, already a dependency, then `heif-convert`) as the primary path, which works with no GUI session and matches Docker Immich's own libvips+libde265 output; `sips` remains a last-resort fallback for a logged-in desktop. Each decoder's output is validated before use, and the shim logs which decoders it resolved at startup.

## 1.5.27 — 2026-07-09

### Fixed
- **Immich 3.0 core plugin failed to import** (`Failed to import plugin from /build/plugins/immich-plugin-core`): the v1.5.24 layer-download early-exit treated `plugins/<name>/dist/plugin.wasm` as "plugin complete", but 3.0 ships the plugin's `manifest.json` (at the plugin root) in a separate image layer. Extraction stopped after the wasm layer and dropped the manifest, so the worker could not import the plugin and workflow features were unavailable. The fix has two parts: `_build_has_core_plugin` now requires BOTH the manifest and the wasm before the early-exit fires; and the cached-server check now verifies the plugin actually belongs to the version being served before trusting the cache, so a broken 3.0 install self-heals on the next start after upgrading. Because `build-data` is a single shared directory that only reflects the last-extracted version, that check is keyed on a version stamp written to `build-data` once it is complete, which avoids both trusting a stale cross-version plugin and needlessly re-downloading a cache that is fine. Existing installs (whose complete `build-data` predates the stamp) and offline `--import-server` users are adopted in place rather than re-downloaded, by matching the plugin against the requested version's layout, so upgrading is free. Applies to the ghcr download, local-Docker extraction, and offline import paths. Found by dogfooding 3.0.

## 1.5.26 — 2026-07-09

### Fixed
- **Motion-photo / video metadata errors on Immich 3.0** (#95): `probePackets` failed with `TypeError: Cannot read properties of undefined (reading 'split')`. The pg_dump shim wraps `child_process.execFile` but dropped its `util.promisify.custom` symbol, so Immich 3.0's `const execFile = promisify(execFileCb)` fell back to the default promisifier and resolved to a bare stdout string instead of `{ stdout, stderr }`; destructuring `{ stdout }` then yielded `undefined`. The shim now reinstalls a custom promisifier so `promisify(execFile)` keeps the `{ stdout, stderr }` contract. A 3.0-only codepath, latent until now. Reported by [@shtefko](https://github.com/shtefko).

## 1.5.25 — 2026-07-05

### Fixed
- **Worker crash from file-descriptor exhaustion** (#89): Immich leaks file handles while processing some media (a reporter hit it on Sony A7IV XAVC files on an external drive), opening each source file and never closing it. His diagnostic (from v1.5.23) showed ~49,000 open descriptors, all pointing at the source files; once the fd table is that large macOS fails `spawn()` with `EBADF` and the worker crashes. The watcher now monitors the worker's live open-fd count (summed across worker processes, via libproc) and restarts the worker before the table gets dangerous (default 10,000; a healthy worker sits around 150), with a cooldown to avoid thrash. Tunable via `IMMICH_ACCEL_FD_RESTART_THRESHOLD` (0 disables) and `IMMICH_ACCEL_FD_RESTART_COOLDOWN`. This is a safety net for an upstream Immich leak, not a fix for the leak itself.

## 1.5.24 — 2026-07-03

### Immich 3.0 support
- **Plugin-path detection** now recognizes both the 2.7 layout (`corePlugin/`) and the 3.0 layout (`plugins/immich-plugin-core/`). Immich 3.0 renamed the WASM core-plugin directory, which made the layer-download early-exit never fire on 3.0, so setup downloaded every image layer instead of stopping once the small plugin layer arrived. Setup is fast again on 3.0. The rest of the 3.0 worker (extraction, native Sharp, ffmpeg wrapper, plugin load, worker start) was verified end to end.
- README notes the worker tracks your Immich version, verified with 2.7.x and 3.0.x.

Note: moving your own Docker stack from 2.7 to 3.0 requires Immich's database migration (pgvecto-rs to VectorChord); that is on the Immich side, not the accelerator.

## 1.5.23 — 2026-07-03

### Diagnostics
- **`spawn EBADF` instrumentation** (#89): when a child-process spawn throws (the crash a reporter hits during ffmpeg capability detection, not reproducible on any environment we can build), the shim now logs the file-descriptor state at the point of failure, which of fd 0/1/2 is invalid, the requested stdio, and the process's open fds, then re-throws unchanged. Fires only on the error path; silent in normal operation. This is to capture the root cause on the affected machine.

## 1.5.22 — 2026-07-02

### Fixed
- **pg_dump shim wrapped `child_process` spawn twice** (#89): the shim called `install()` for both `child_process` and `node:child_process`, which are the same object on modern Node, so every spawn ran through two nested wrappers (two `pg_dump_shim.js` frames in stack traces). Now guarded to patch a given module object once. This is hardening, not a confirmed fix for the reported `spawn EBADF`, which could not be reproduced across Node 22/24, launchd-style stdio, high concurrency, or starved fd limits, and rewrites nothing for ffprobe. Reported by [@KoenM9264](https://github.com/KoenM9264).

## 1.5.21 — 2026-06-27

### Fixed
- **`brew cleanup` failing on root-owned `.pyc`** (#86): the CLI wrapper now sets `PYTHONDONTWRITEBYTECODE=1`, so Python no longer writes `__pycache__` into the Homebrew Cellar. Previously, running the CLI as root (e.g. `sudo immich-accelerator ...`) left root-owned bytecode in the keg that `brew cleanup` couldn't remove. Reported by [@shtefko](https://github.com/shtefko). After upgrading, clear any existing root-owned files once: `sudo chown -R $(whoami):admin /opt/homebrew/Cellar/immich-accelerator`.

## 1.5.20 — 2026-06-26

### Docs
- **README house style**: added badges, a Star History chart, and License/Credits sections, and aligned the footer to the canonical form. Documented that Immich's `Transcoding video ... without hardware acceleration` log is expected and benign on macOS (the ffmpeg wrapper does the VideoToolbox encode; Immich has no VideoToolbox option to report). Reported by [@shtefko](https://github.com/shtefko) (#84).
- **README slimmed**: Troubleshooting is now a collapsible symptom index (`<details>`), and the Split deployment guide moved to `docs/split-deployment.md` with a short pointer. The common path reads top-to-bottom without scrolling past the long tail.
- **README polish**: promoted the status from Alpha to Beta (in daily use), de-duplicated the Commands table (the `immich-accelerator` prefix is stated once, not on every row), and removed all em-dashes.

## 1.5.19 — 2026-06-24

### Fixes
- **`brew services stop`/`restart` now stop ML and the dashboard too, not just the worker (#81 follow-up).** v1.5.18's stop handler used the normal sequential shutdown, which waits up to 5s per service; launchd SIGKILLs the watcher only a few seconds after signalling it, so the handler was cut off after the worker and left ML + dashboard running. The handler now signals all services up front, so they all stop even under launchd's short grace.

## 1.5.18 — 2026-06-24

### Fixes
- **Media-readiness gate no longer refuses a writable split setup (#80).** The gate checked writability at the media *root*, but Immich only writes to the subdirs (`upload`, `thumbs`, …) — a split deployment can have a root-owned, non-writable media root (e.g. a `/data` synthetic link) while the subdirs are fully writable. The gate now places/verifies its marker across the media root *and* Immich's standard subdirs (root first, for back-compat), testing writability where Immich actually writes. A genuinely missing/unmounted root still correctly refuses. Reported by [@shtefko](https://github.com/shtefko).
- **`brew services stop`/`restart` now actually stop the worker (#81).** The worker, ML, and dashboard run detached, so they survived when launchd signalled only the watcher — a stop or restart left them running. The watcher now traps SIGTERM/SIGINT and stops its services before exiting, so the service lifecycle behaves as expected. Reported by [@shtefko](https://github.com/shtefko).
- **`immich-accelerator start` now also starts the dashboard.** Previously only `watch`/brew-services brought it up. Reported by [@shtefko](https://github.com/shtefko).

## 1.5.17 — 2026-06-23

### Fixes
- **`brew upgrade` now actually takes effect.** The worker and ML service run detached, so they survived a `brew services restart` — a fresh watcher would adopt the still-running *old-code* worker, and an upgrade silently didn't apply until a full manual stop/start. The watcher now stamps the version each worker starts with, restarts a worker found running stale code, and (in service mode) notices when the installed version on disk changes and relaunches itself so the new code — shims and fixes — comes up on a fresh worker within ~30s. No manual restart needed when running as a service. README updated.

## 1.5.16 — 2026-06-23

### Features
- **Media-readiness gate: don't start until the real media root is mounted (default on).** On a network-mounted media root, the worker could start before the mount came up and write thumbnails into a local placeholder directory that the mount later masks (silent data loss). The worker now drops a small marker file in the media root on first start and verifies it on every subsequent start; if the marker is missing — a placeholder, or the mount isn't up yet — it refuses to start (the watch loop retries, so it comes up as soon as the mount appears). Mount-agnostic (works for local, NFS, and SMB), runs before the ML service so a not-ready mount never orphans it, and probes in a timeout-bounded subprocess so a hung mount can't wedge startup. Opt out with `"require_media_ready": false`.

## 1.5.15 — 2026-06-23

### Fixes
- **Keep remote Postgres connections alive in split deployments (#74).** When the worker and the database sit on different network segments, a stateful firewall or NAT can silently reap an idle connection; the worker then hangs on the next read until `read ETIMEDOUT` and doesn't recover (intermittent, since only idle connections get reaped). Immich doesn't expose a keepalive setting, so the worker now preloads a small shim that enables TCP keepAlive on every `pg` connection, keeping the socket warm so it isn't dropped. No-op for same-host setups; Immich's source is untouched. Tunable/disable via `IMMICH_ACCEL_PG_KEEPALIVE` / `IMMICH_ACCEL_PG_KEEPALIVE_MS`. Reported by [@shtefko](https://github.com/shtefko).

## 1.5.14 — 2026-06-23

### Fixes
- **Actionable error when a fresh split deployment can't import geodata (#73).** On a split setup whose frontend ran api-only from the start, the accelerator is the first microservices worker to run, so it attempts Immich's one-time reverse-geocoding (geodata) import — a large bulk insert that can break over a network DB connection (`write EPIPE`), leaving the worker in a silent crash-restart loop. The worker log is now scanned on failure: when this signature is detected, the accelerator prints the cause and the fix (initialize geodata once on the frontend, then switch back to `IMMICH_WORKERS_INCLUDE=api`) instead of a raw stack trace. Documented in the split-deployment guide. Reported by [@shtefko](https://github.com/shtefko).

## 1.5.13 — 2026-06-21

### Fixes
- **Fix misplaced OCR bounding boxes; sync the ML service to upstream.** OCR boxes were emitted in pixel coordinates, but Immich expects normalized 0–1 coordinates, so recognized-text boxes rendered in the wrong place. The bundled `immich-ml-metal` is updated to upstream `main` (carrying our `mlx<0.31.2` pin), which fixes the bbox calculation and brings ~2 months of upstream improvements: ViT-L-14 auto-conversion to MLX with an open_clip fallback, face-recognition CoreML provider tuning (MLProgram/ANE), CLIP model load moved off the event loop, and new model mappings. Reported by [@shtefko](https://github.com/shtefko) (immich-ml-metal#2).

## 1.5.12 — 2026-06-19

### Fixes
- **Cap service logs so they can't grow unbounded.** `worker.log`/`ml.log` were opened in append mode and never rotated — a worker spewing stack traces for unsupported files (e.g. videos mislabeled `.heic`) had grown one log to 10GB. Each log is now capped at 200MB: the `watch` loop rotates in place every cycle, and `start` rotates before (re)opening. Rotation truncates the existing file rather than renaming it, so it works while the worker holds the file open (append mode), and the last 2000 lines of context are preserved.

## 1.5.11 — 2026-06-16

### Fixes
- **Dashboard progress now matches Immich's own counts (#68)**. Per-stage completion could read over 100% (e.g. "Neural Engine — 102.1%") and was generally inaccurate: the done-counts were taken from side tables (`smart_search`, `asset_job_status`) unfiltered — including rows left behind by deleted/hidden assets — and divided by a single asset total. Each bar now counts over the **same population Immich itself uses** (per `asset-job.repository.js`): thumbnails/OCR over live non-hidden assets, CLIP/faces over assets-that-have-a-preview, video over live non-hidden videos. Done can no longer exceed total, and the percentages line up with Immich's Jobs page. (A 100% clamp remains as a safety net.) Reported by [@Rustymage](https://github.com/Rustymage).

## 1.5.10 — 2026-06-14

### Fixes
- **Generate thumbnails for HEIC photos (#62)**. Sharp's prebuilt libvips on macOS bundles libheif without an HEVC decoder (AVIF only), so iPhone HEICs failed to decode (`bad seek` / "compression format has not been built in") and never got thumbnails. The native worker now routes HEVC-HEIC files through Apple's ImageIO (`sips`) before Sharp, via a preloaded module that wraps `sharp` — Immich's source is untouched. Handles tiled iPhone HEICs that even system libheif rejects (its iref security limit). AVIF, JPEG, and everything else are unaffected. Reported by [@goldhandconsultancy](https://github.com/goldhandconsultancy).

## 1.5.9 — 2026-06-14

### Fixes
- **Fix a regression in 1.5.8's upload detection (#62, #64)**. 1.5.8 returned the resolved media location as the detected value, which combined with the start-time path check to block `start` with a false "path mismatch" even after a correct setup. Detection now keeps the raw `IMMICH_MEDIA_LOCATION` value (the effective path is still used internally to pick the mount), and only bind mounts are considered. Upgrading from 1.5.8 is recommended.

## 1.5.8 — 2026-06-14

### Fixes
- **Detect the upload mount when uploads live at `/data` (#62)**. Detection only matched a Docker mount whose destination contained `/upload`, so the modern Immich default — `${UPLOAD_LOCATION}:/data` with no `IMMICH_MEDIA_LOCATION` set — reported "Upload: not detected". Detection now resolves the media location the way Immich itself does (`storage.service.js`: explicit `IMMICH_MEDIA_LOCATION`, else whichever of `/data` or `/usr/src/app/upload` is mounted) and reports the matching mount. Reported by [@goldhandconsultancy](https://github.com/goldhandconsultancy).

## 1.5.7 — 2026-06-13

### Fixes
- **Detect the media root correctly when a storage label is set (#61)**. The split-deployment path check assumed Immich's `upload/<uuid>/` layout and mis-detected a date-nested folder (e.g. `…/library/Anthony/2026/06`) for anyone using a storage label or custom storage template — the suggested `upload_mount` would have gone stale next month. Detection now follows Immich's own path builder (`<MEDIA>/library/<label|uuid>/…`) and resolves the true media root.
- **Don't suggest an impossible synthetic link (#61)**. macOS synthetic links can only create a single top-level name, so the previous guidance to mirror Docker's container-default `/usr/src/app/upload` could never work. The mismatch warning now offers achievable routes: a synthetic link when the media root is top-level, or re-pointing `IMMICH_MEDIA_LOCATION` first when it isn't. Suggested commands use `printf` so the tab separator is portable across bash and zsh.
- **Validate the `/build` synthetic entry by content, not file existence (#61)**. A hand-edited `/etc/synthetic.d/immich-accelerator` (e.g. a manual upload-path entry) was treated as "build link configured," so the required `/build` entry was silently skipped and Microservices came up red after reboot. Setup now checks for the actual `build` entry and appends it if missing, preserving any foreign lines. Reported by [@Rustymage](https://github.com/Rustymage).

## 1.5.6 — 2026-06-11

### Fixes
- **Default local DB/Redis host to 127.0.0.1 instead of localhost (#59)**. On macOS, `localhost` can resolve to `::1` (IPv6) first while the generated Docker stack publishes Postgres/Redis on `127.0.0.1` only, so connections could fail or stall. Auto-detected local setups now use `127.0.0.1`; remote and manual configs are unchanged. Contributed by [@hiisukun](https://github.com/hiisukun).

### CI
- **Bump GitHub Actions to Node 24 runtimes (#60)**. `checkout` v4 → v6, `setup-python` v5 → v6, ahead of GitHub's June 16 forced Node 24 migration.

## 1.5.5 — 2026-06-10

### Fixes
- **Fix install and upgrade flow for Homebrew 5.1.15+ tap trust enforcement.** Homebrew now refuses to load formulas from untrusted third-party taps: `brew install immich-accelerator` after a bare `brew tap` hard-fails for new users, and existing taps are silently skipped by `brew outdated`/`brew upgrade` — installs go stale with no warning. Quick start now uses the fully-qualified `brew install epheterson/immich-accelerator/immich-accelerator` (which Homebrew accepts without prior trust) plus `brew trust` so upgrades keep working. The formula caveats now mention the trust step, and the README documents both failure modes and how to update the accelerator itself.

## 1.5.4 — 2026-06-09

### Features
- **Support authenticated Redis (#56)**. The native worker now forwards `REDIS_PASSWORD` and `REDIS_USERNAME` to Immich, so deployments that require a Redis password (e.g. TrueNAS) or ACL auth work. Contributed by [@exkuretrol](https://github.com/exkuretrol).

## 1.5.3 — 2026-06-03

### Fixes
- **Fix root-owned files from Docker containers (#52)**. Fresh installs now offer to run the Immich server container as the current user, preventing root-owned files in bind-mounted directories. Contributed by [@hiisukun](https://github.com/hiisukun).
- **Graceful uninstall when root-owned files exist**. `uninstall` now explains the problem and suggests `sudo rm -rf` instead of crashing with a raw PermissionError.
- **Fix `/etc/synthetic.d` permissions**. A tight root umask could leave the directory unsearchable by non-root users, breaking the `/build` firmlink check.
- **Compose project named "immich"**. Was defaulting to "docker" (the parent directory name).

## 1.5.2 — 2026-06-02

### Fixes
- **Fix watchdog spawning duplicate workers (#51)**. Immich 2.7+ sets `process.title='immich'`, so the parent node PID exits while child processes keep running. PID tracking now falls back to scanning for live `immich` processes, preventing the watchdog from spawning duplicates. Stop also kills all orphaned immich processes.

## 1.5.1 — 2026-05-25

### Fixes
- **HEVC VideoToolbox output now always uses Apple-compatible hvc1 tag (#48)**. ffmpeg defaults to hev1 for HEVC, which Apple's decoder rejects. The wrapper now injects `-tag:v hvc1` when encoding HEVC via VideoToolbox if the caller doesn't specify it.

## 1.5.0 — 2026-05-19

### Features
- **One-command fresh install.** `immich-accelerator setup` on a bare Mac now sets up everything — installs OrbStack if no Docker is found, creates the Immich Docker stack, configures the native worker, and starts all services. Two questions: where your photos are, where Immich should store its data. No manual docker-compose editing.
- **Managed Docker stack.** The generated compose file lives at `~/.immich-accelerator/docker/`. Re-running setup detects it and restarts if stopped, without re-prompting.

## 1.4.13 — 2026-05-18

### Fixes
- **UHDR JPEG thumbnails failing (#44)**. Sharp was building from source against system libvips which has a UHDR loader that breaks JPEG auto-detect. Now installs the official pre-built darwin-arm64 binary from npm, which bundles vips 8.17.3 without the UHDR loader. Matches stock Immich Docker behavior. Existing installs: `immich-accelerator setup` to trigger a rebuild.

## 1.4.12 — 2026-05-16

### Fixes
- **Startup validates database and Redis connections with real queries (#42)**. Instead of just checking TCP connectivity, the preflight now runs `psql SELECT 1` and `redis-cli PING`. Surfaces the actual error (auth failure, port conflict, connection reset) with actionable guidance instead of letting the worker crash with a raw ECONNRESET.
- **Auto-creates missing media subdirectories on startup (#43)**. If `upload/`, `thumbs/`, `encoded-video/`, etc. are missing under `IMMICH_MEDIA_LOCATION`, the preflight creates them with `.immich` markers so Immich's StorageService doesn't crash.

## 1.4.11 — 2026-04-27

### Improvements
- **Environment preflight checks on start.** Auto-detects and fixes common issues before launching the worker: ImageMagick HEIC codec (auto-reinstalls), NFS mount accessibility, DB/Redis connectivity for split setups. Runs silently when everything is healthy.

## 1.4.10 — 2026-04-25

### Fixes
- **CLIP/smart search crashes on MLX 0.31.2 (#38)**. MLX 0.31.2 introduced a threading regression that crashes with "no Stream(gpu, 0)" during CLIP inference. Pinned `mlx<0.31.2` until upstream fixes it. Existing installs: `brew reinstall immich-accelerator`.
- **ML submodule updated** with face embedding batch-dim fallback, CLIP model-swap retry, and gpu_lock clarification from upstream code review.

## 1.4.9 — 2026-04-23

### Fixes
- **iPhone 15+ Ultra HDR images failed thumbnail generation (#36)**. Sharp was using a pre-packaged binary without libultrahdr support. Now builds from source against Homebrew's libvips which includes it. Existing installs: run `immich-accelerator setup` to trigger a rebuild.

## 1.4.8 — 2026-04-18

### Improvements
- **Dashboard shows worker memory usage.** "Microservices Worker (450 MB)" next to the service status so memory growth during long thumbnail runs is visible (#33).
- **Human-readable API error messages.** Dashboard now shows specific messages for common failures (auth rejected, connection refused, timeout, empty response) instead of raw Python exceptions.

## 1.4.7 — 2026-04-17

### Fixes
- **Homebrew formula failed to install on every release since v1.4.4 (#31)**. The CI workflow that generates the formula used a bash heredoc containing backticks and em-dashes. Backticks triggered command substitution on the runner, em-dashes got mangled through the locale. Brew's Ruby installer rejected the resulting invalid UTF-8. Replaced with plain ASCII.

## 1.4.6 — 2026-04-16

### Fixes
- **ML service silently failed to start after brew upgrade (#29)**. Config stored a versioned Cellar path for `ml_dir` which gets deleted on upgrade. `cmd_start` and `cmd_watch` now auto-resolve `ml_dir` via the stable `/opt/homebrew/opt/` symlink on every run. Warns loudly when the ML venv is missing instead of silently skipping.
- **Dashboard showed "Idle" while Immich was processing (#28)**. The `/api/jobs` call had a 2s timeout that caused intermittent failures under load, and errors were silently swallowed. Dashboard now shows the actual error when the API is unreachable, displays queue counts matching Immich's admin panel, and bumps the timeout to 5s.

## 1.4.5 — 2026-04-15

### Fixes
- **Database backups were silently 0 bytes (#24)**. Immich pipes `pg_dump` through `gzip --rsyncable`, which Apple's BSD gzip doesn't support. `pg_dump_shim.js` now reroutes those calls to Homebrew's GNU gzip (or strips the flag as fallback). The formula now `depends_on "gzip"` so fresh installs get GNU gzip automatically.
- **Watchdog could kill unrelated processes**. `_kill_stale_processes` matched any command line containing the substring `immich`. Replaced with precise patterns for the canonical worker (`node … dist/main.js`) and ML service (`python -m src.main`) launch shapes.

### Test infrastructure
- **Isolated E2E Immich stack** (`scripts/e2e-stack.yml` + `e2e-stack.sh`). Dedicated postgres / redis / api-only Immich server on port-shifted loopback addresses with throwaway state. The VM harness now requires this stack and refuses to run against prod Immich.
- **Real-data backup integration test** that runs `pg_dump | gzip --rsyncable` through the shim against the isolated Postgres and asserts the output is a valid non-empty pg_dump.

## 1.4.4 — 2026-04-14

### Fixes
- **Homebrew formula pulled node 25, breaking sharp on every fresh install**: v1.4.x shipped with `depends_on "node"` in the generated formula. Homebrew's default `node` formula tracks mainline (currently 25.x), but Immich 2.7.x pins `engines.node = 24.14.1` and `sharp@0.34.5`'s native addons fail to load on node 25 with a `NODE_MODULE_VERSION` mismatch. Every fresh `brew install immich-accelerator` + `immich-accelerator start` hit an opaque worker crash at `require('sharp')` mid-Nest-bootstrap that looked like an Immich bug. Fix: pin `depends_on "node@22"` in the formula template and teach `find_node()` to look under `/opt/homebrew/opt/node@22/bin/node` (keg-only — no `/opt/homebrew/bin` symlink).
- **`find_node()` accepted unsupported node majors silently**: previously it returned the first `node` binary it found, regardless of version. Now filters to `SUPPORTED_NODE_MAJORS = (22, 24)` and installs `node@22` if nothing compatible is present.
- **`_rebuild_sharp()` swallowed rebuild failures**: logged `log.error` and returned, letting the worker start and crash later with an unrelated-looking stack. Now raises `RuntimeError` with the rebuild stderr tail and a remediation pointing at `brew install node@22`.

### Upgrade resilience (catches future drift)
- **Node preflight in `start`**: every `immich-accelerator start` now re-resolves node via `find_node()`, updates `config["node"]` if the path changed, and compares against Immich's `engines.node` parsed from `package.json`. Catches the `brew upgrade` drift pattern where node silently jumps majors and breaks sharp, with a clear "install node@22" error before the worker ever spawns.
- **Sharp load preflight in `start`**: spawns `node -e "require('sharp')"` against the server dir. If it fails, auto-attempts a rebuild and retries. If the retry still fails, hard-errors with remediation. This turns "opaque worker crash 10+ seconds into Nest bootstrap" into a 1-second clearly-labeled check.

### Test coverage
- `tests/test_fresh_install.py::TestNodeVersionPreflight` — 11 new unit tests covering `_node_major_version`, `find_node` version filtering, `_check_node_engines_compat` with real stubbed node binaries, `_verify_sharp_loads` error reporting, and a static check that the CI-generated Homebrew formula pins `node@22`.
- `scripts/e2e-fresh-install.sh` step 1b — asserts `find_node()` on a real VM returns a node in `SUPPORTED_NODE_MAJORS`. Would have caught the v1.4.x regression on the first E2E run.
- `scripts/e2e-fresh-install.sh` step 3b — runs the full sharp preflight (`_check_node_engines_compat` + `_verify_sharp_loads`) against the extracted Immich server in the VM.
- `scripts/e2e-fresh-install.sh` steps 9–16 — **real execution coverage**: start ML in `STUB_MODE`, hit `/ping` + `/health` + `/predict` for actual JSON render (catches any `ORJSONResponse` regression), run `immich-accelerator ml-test` against the stub, verify the `NODE_OPTIONS` pg_dump shim actually loads in a real node process, start the real worker and wait for the `Immich Microservices is running` Nest-bootstrap marker, run the dashboard against the live worker and confirm `worker.alive=true`, verify the `status` subcommand reports the running PID, verify `stop` cleanly terminates + is idempotent, verify a second `start` after `stop` reaches Nest bootstrap again.
- `scripts/e2e-bootstrap-vm.sh` — installs `node@22` instead of Homebrew-default `node`, sets up the `/build` synthetic firmlink via dual `/etc/synthetic.d/` + `/etc/synthetic.conf` entries (some macOS VM images only honor the legacy location), reboots the VM to activate the firmlink, and verifies `/build` resolves post-reboot before saving the base snapshot. pip install now retries 3× on VM DNS flakes.

## 1.4.3 — 2026-04-14

### Fixes
- **NODE_OPTIONS shim path broken in v1.4.2 (#24 follow-up)**: The v1.4.2 commit that "polished" the pg_dump shim wrapping wrapped the path in single quotes, thinking Node would unquote them. Node doesn't — it splits `NODE_OPTIONS` on whitespace and takes the literal characters. The quotes became part of the filename and every worker start failed with `Cannot find module ''/opt/homebrew/Cellar/…/pg_dump_shim.js''`. Fix: wrap the path in **double** quotes, which is the only form Node's `NODE_OPTIONS` tokenizer honors universally (single quotes and backslash escapes both fail — both verified empirically against Node 25.2). Verified end-to-end against a real v1.4.2 brew install on an M4 Mac — shim loads, `pg_dump (PostgreSQL) 18.3` runs.
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
