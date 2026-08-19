# Immich Accelerator

[![CI](https://github.com/epheterson/immich-apple-silicon/actions/workflows/ci.yml/badge.svg)](https://github.com/epheterson/immich-apple-silicon/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/epheterson/immich-apple-silicon.svg?label=release)](https://github.com/epheterson/immich-apple-silicon/releases)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-Apple%20Silicon-blue.svg)]()
[![Homebrew](https://img.shields.io/badge/install-Homebrew-orange.svg)](https://github.com/epheterson/homebrew-immich-accelerator)
[![Immich](https://img.shields.io/badge/Immich-2.7%2B-5b21b6.svg)](https://immich.app/)

> **Beta.** In daily use on a Mac Mini M4 (24GB) against an Immich 3.0.x library. Stable, but back up your Immich database before your first run.

Run Immich's compute natively on Apple Silicon. Thumbnails use the fast M-series CPU, video transcoding uses VideoToolbox hardware encoding, and ML runs on Metal GPU, Neural Engine, and CoreML.

Docker handles the lightweight parts (API server, Postgres, Redis). The accelerator runs Immich's own microservices worker and/or ML service natively on macOS, giving either access to hardware that Docker can't reach.

## How it works

**Worker and ML are independent processes, not a package deal.** Pick the mode below that matches where your Docker stack already lives and how much of the compute you want this Mac to take on.

### Same machine

Docker and the accelerator on one Mac — the default, and what [Quick start](#quick-start) below sets up.

```
Docker (lightweight)                Native macOS (compute)
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

### Split: worker + ML on a remote host

Docker stays where it already lives (a NAS, another server); the worker and ML both move to the Mac. Full guide: [docs/deployment.md](docs/deployment.md#split-deployment--worker--ml-on-a-remote-host-nas--mac-or-any-two-hosts).

```
Host A: Docker (NAS, etc.)          Host B: Mac (compute)
+-----------------------+           +-------------------------------+
|  immich-server (API)  |           |  Immich Accelerator           |
|  postgres             |<--------->|  +- Microservices worker      |
|  redis                | DB+Redis  |  |  +- Sharp (thumbnails)     |
|                       |           |  |  +- ffmpeg (VideoToolbox)  |
|  WORKERS_INCLUDE=api  |           |  +- ML service                |
|  ML_URL=mac-ip:3003   |           |     +- CLIP (MLX/Metal)       |
+-----------------------+           |     +- Faces (Vision/ANE)     |
                                    |     +- OCR (Vision/ANE)       |
                                    +-------------------------------+
```

**Extra requirement:** both hosts must see your library at the same absolute path via a shared filesystem (not pictured) — the worker reads and writes photos directly, no HTTP transport. That requirement comes from the **worker**, not from ML.

### ML-only: dedicate a spare Mac to ML compute

Docker _and_ the worker stay wherever your Immich already runs, unmodified. This Mac only answers ML requests over plain HTTP. Full guide: [docs/deployment.md#ml-only-network-node](docs/deployment.md#ml-only-network-node).

```
Docker, anywhere (unmodified)       Spare Mac (ML compute only)
+-----------------------+           +---------------------------------+
|  immich-server (API)  |--HTTP-->  |  Immich Accelerator             |
|  postgres             |  :3003    |  (--ml-only)                    |
|  redis                |           |                                 |
|  microservices worker |           |  +- ML service only             |
|  (thumbnails, video,  |           |     +- CLIP (MLX/Metal)         |
|   metadata, RAW/HEIC) |           |     +- Faces (Vision/ANE)       |
|                       |           |     +- OCR (Vision/ANE)         |
|  ML_URL=mac-ip:3003   |           |                                 |
+-----------------------+           |  no worker, no Docker,          |
                                    |  no library mount               |
                                    +---------------------------------+
```

**No extra requirement.** The ML service never touches your library on disk — only the image bytes and text sent to it — so there's no shared filesystem to align. If your actual goal is "give my Immich instance more ML horsepower," this is almost always the mode you want, not the split above.

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

For NAS + Mac setups, see [Split: worker + ML on a remote host](#split-worker--ml-on-a-remote-host) above. For `IMMICH_MEDIA_LOCATION` details and moving thumbnails/transcodes to faster storage, see [docs/usage.md#configuration-details](docs/usage.md#configuration-details).

Run the accelerator as a background service rather than a bare `start` — see [docs/usage.md#running-as-a-service-recommended](docs/usage.md#running-as-a-service-recommended) for auto-restart, auto-update, and log rotation.

## Documentation

Everything past the quick start lives in [`docs/`](docs/):

| Doc                                                    | Covers                                                                                                                                                  |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [docs/usage.md](docs/usage.md)                         | Day-to-day operation: full CLI reference, dashboard & menu bar app, updates, running as a service, performance tuning, component toggles, configuration |
| [docs/deployment.md](docs/deployment.md)               | Split deployment (worker + ML remote) and ML-only network nodes, in full detail                                                                         |
| [docs/ml-engine.md](docs/ml-engine.md)                 | Native Swift vs. Python ML engine, model zoo, switching engines                                                                                         |
| [docs/known-differences.md](docs/known-differences.md) | Where native output can differ from Docker, and why it usually doesn't matter                                                                           |
| [docs/troubleshooting.md](docs/troubleshooting.md)     | Symptom → cause → fix for common setup and runtime issues                                                                                               |
| [docs/security.md](docs/security.md)                   | Safety guarantees and every network-facing surface                                                                                                      |
| [CONTRIBUTING.md](CONTRIBUTING.md)                     | Testing requirements and PR guidelines                                                                                                                  |

## Repo layout

```text
immich-apple-silicon/
├── immich_accelerator/  # Python CLI: setup wizard, worker/ML process management, dashboard (FastAPI+uvicorn), ffmpeg-wrapper.sh, install hooks
├── native-ml/           # Swift package: native ML engine (mlx-swift CLIP, Apple Vision faces/OCR, onnxruntime for the ONNX zoo + ArcFace)
├── menubar/             # SwiftUI menu-bar app: status at a glance, start/stop/restart, ml-test, logs
├── ml/                  # Git submodule: Python ML service fallback (managed fork of immich-ml-metal)
├── docker/              # docker-compose.yml template, rendered into a user's install with their paths/env vars
├── launchd/             # LaunchAgent plist for non-Homebrew (git checkout) installs
├── scripts/             # E2E VM install harness, real-model ML preflight gates, benchmarks, Homebrew formula renderer
├── tests/               # pytest suite for immich_accelerator/ (compose templates, config parsing, fresh-install regressions)
├── docs/                # User-facing docs (table above), plus docs/testing/ and docs/plans/
├── CLAUDE.md            # Working agreement for AI-assisted changes (git workflow, ML preflight gate, code style)
├── CONTRIBUTING.md      # Human contribution requirements
├── CHANGELOG.md         # Release notes
└── VERSION              # Current version, tagged vX.Y.Z
```

## What we modify (and how to undo it)

**Nothing inside Docker is modified.** We don't patch Immich, rebuild images, or replace containers. All changes are to your `docker-compose.yml` and can be reverted by removing a few lines.

| What we change                 | How                                                                              | Reversible?                                                     | Risk |
| ------------------------------ | -------------------------------------------------------------------------------- | --------------------------------------------------------------- | ---- |
| Add env vars to docker-compose | `IMMICH_WORKERS_INCLUDE`, `IMMICH_MACHINE_LEARNING_URL`, `IMMICH_MEDIA_LOCATION` | Remove the lines                                                | None |
| Expose Postgres/Redis ports    | `5432:5432`, `6379:6379` in docker-compose                                       | Remove the port lines                                           | None |
| Native microservices worker    | Extracted from Docker image, runs via `node`                                     | Stop the accelerator                                            | None |
| Native ML service              | Native Swift engine (Python venv fallback)                                       | Stop the accelerator                                            | None |
| `/build` symlink (Immich 2.7+) | `/etc/synthetic.d/immich-accelerator` (requires sudo once during setup)          | `immich-accelerator uninstall` removes it; reboot to deactivate | Low  |

**Why `/build`?** Immich 2.7+ stores absolute plugin paths like `/build/corePlugin/dist/plugin.wasm` in its database. Both Docker and native workers need `/build` to resolve. macOS SIP prevents creating root-level directories, so we use Apple's [synthetic link](<https://man.cx/synthetic.conf(5)>) mechanism to map `/build` → `~/.immich-accelerator/build-data`. Setup prompts for sudo once; a reboot may be required to activate.

**To fully revert:** Stop the accelerator, remove the env vars and port mappings from docker-compose, `docker compose up -d`. Immich is back to stock.

The native worker runs Immich's own code with the same UPSERT-safe writes, and the extracted server always matches the Docker image version exactly — see [docs/security.md](docs/security.md) for the full safety and network-surface rundown.

## On agentic engineering

This project is built with [Claude Code](https://claude.com/claude-code), from zero knowledge of the Immich codebase to a working native accelerator, including upstream contributions to the ML service and a feature discussion with the Immich maintainers. It has since had contributions from other people too. Inspect the code yourself, use it and share it, or don't.

---

## Contributors

[![Contributors](https://contrib.rocks/image?repo=epheterson/immich-apple-silicon)](https://github.com/epheterson/immich-apple-silicon/graphs/contributors)

<!-- Star History: GitHub restricted the stargazers API in August 2026, so the
     chart currently renders as "GitHub restricted access to star data" for
     every repo, not just this one. Uncomment when that is resolved.

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=epheterson/immich-apple-silicon&type=Date)](https://star-history.com/#epheterson/immich-apple-silicon&Date)
-->

---

## License

MIT

## Credits

Built on [Immich](https://immich.app/) · [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal) · [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg) · [Sharp](https://sharp.pixelplumbing.com/)

Two projects we learned from: the menu-bar app's design is inspired by [Immich-Accelerator-Helper](https://github.com/pl4za/Immich-Accelerator-Helper) by [@pl4za](https://github.com/pl4za), and running Immich's own ONNX model exports natively via onnxruntime was informed by [michina-swift](https://github.com/lucka-me/michina-swift) by [@lucka-me](https://github.com/lucka-me).

---

Built with ❤️ in California by [@epheterson](https://github.com/epheterson) and [Claude Code](https://claude.com/claude-code).
