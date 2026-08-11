# Repo layout

What each top-level directory is, what language/tooling it uses, and how it's built or tested. For what the *product* does, start with [README.md](README.md); this file is about the repo itself.

## `immich_accelerator/`

The core Python CLI package (`immich-accelerator setup|start|stop|watch|dashboard|...`). Runs the setup wizard, extracts and manages the native worker process, launches the ML engine, and serves the web dashboard (`dashboard.py` + `dashboard.html`, FastAPI/uvicorn). `ffmpeg-wrapper.sh` is the bash shim that remaps Immich's software encoder requests to VideoToolbox hardware encoders — see the "Code style" note in [CLAUDE.md](CLAUDE.md) for why it's kept minimal. `hooks/` holds install-time hooks invoked by the Homebrew formula. Tested by `pytest` (see [`tests/`](tests/) below).

## `native-ml/`

Swift package for the native ML engine — a single compiled binary (mlx-swift for CLIP, Apple Vision for face detection/OCR, onnxruntime for the ONNX CLIP zoo and ArcFace face recognition) that replaces the Python ML service by default since v1.6.0. Built with `swift build`; `scripts/` inside this package (`build_bundle.sh`, `install_native.sh`, `release_bundle.sh`) handle producing a self-contained, notarizable bundle. See [native-ml/README.md](native-ml/README.md) for engine internals and parity-testing status.

## `menubar/`

SwiftUI menu-bar app: accelerator health at a glance (worker/ML/dashboard status, NATIVE-or-PYTHON engine badge) plus one-click start/stop/restart/`ml-test`/logs. Reads the accelerator's own state directly (pidfiles, `/ping`, `config.json`) — no extra daemons. Built with `swift build` / Xcode; `scripts/build_app.sh` produces the notarized `.app` shipped via the Homebrew cask. See [menubar/README.md](menubar/README.md).

## `ml/`

Git submodule: a managed fork of [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal), the Python (FastAPI + mlx + onnxruntime + torch) ML service. This is the fallback engine the accelerator falls back to automatically if the native Swift engine's bundle or models are missing, and upstream changes are reviewed before merging — see [docs/ml-engine.md](docs/ml-engine.md) for when each engine runs. Has its own `pytest.ini`, `requirements.txt`, and test suite, independent of the top-level ones.

## `docker/`

Just `docker-compose.yml`, the template setup renders into a user's install directory with their chosen paths and env vars filled in.

## `launchd/`

`com.immich.accelerator.plist`, the LaunchAgent template installed for non-Homebrew (git checkout) installs so the accelerator survives login/reboot — see [docs/usage.md#running-as-a-service-recommended](docs/usage.md#running-as-a-service-recommended). Homebrew installs use the formula's own service definition instead and don't touch this file.

## `scripts/`

Everything that isn't the shipped product: the Tart-VM end-to-end install harness (`e2e-*.sh`, `e2e-stack.yml`), the real-model ML preflight gates (`ml-preflight.py`, `native-ml-preflight.py` — see the non-negotiable gate requirement in [CLAUDE.md](CLAUDE.md)), a SigLIP benchmark script, the Homebrew formula renderer (`render-formula.sh`, used by CI), and VM cleanup (`tart-cleanup.sh`). See [scripts/README.md](scripts/README.md) for what each one does and when it runs.

## `tests/`

The top-level `pytest` suite for `immich_accelerator/`: compose template validation, regex patterns (path handling, process detection), config parsing, and fresh-install regression tests including a full simulated end-to-end flow. Run with `pytest -v -m "not slow"` — see [CONTRIBUTING.md](CONTRIBUTING.md).

## `docs/`

User-facing documentation split by topic (linked from the [README's Documentation table](README.md#documentation)), plus `docs/testing/` (internal notes on the E2E VM harness) and `docs/plans/` (dated design docs and roadmaps — gitignored, local-only, never shipped; see the header on each file for whether it's still current).

## Top-level files

`CLAUDE.md` is the working agreement for AI-assisted changes to this repo (git workflow, the non-negotiable ML preflight gate, code style). `CONTRIBUTING.md` covers human contribution requirements. `CHANGELOG.md` and `VERSION` track releases (tagged `vX.Y.Z`, mirrored to the [Homebrew tap](https://github.com/epheterson/homebrew-immich-accelerator)). `pytest.ini` configures the top-level test suite.
