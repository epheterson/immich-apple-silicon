# Immich Accelerator — Design Document

## What is this?

A macOS app (menu bar + CLI) that runs Immich's microservices worker natively on Apple Silicon, giving it access to VideoToolbox hardware transcoding and fast CPU processing. Paired with a native ML service for Metal/CoreML/Vision acceleration.

Not a fork. Not a reimplementation. A launcher that runs Immich's own code on better hardware.

## The insight

Immich's supported scaling model is microservices replicas — multiple workers sharing the same DB and Redis. We run one of those replicas bare-metal on macOS instead of in a container, so it can reach VideoToolbox, the fast M4 CPU, and system ffmpeg.

## Who uses this

**Setup A — Same Mac (Eric):** Immich Docker on the Mac (Postgres, Redis, API server). Native microservices worker on the same Mac. Docker handles the infrastructure, native handles the compute.

**Setup B — NAS + Mac (flsabourin):** Immich Docker on Synology NAS. Native microservices worker on the Mac. Heavy compute offloaded from weak NAS CPU to M4.

## What it replaces

| Current project | Accelerator |
|----------------|-------------|
| Custom thumbnail worker (direct DB writes, race condition) | Immich's own Sharp, running natively on M4 CPU |
| FFmpeg proxy + wrapper shell scripts | Immich's own ffmpeg calls, using system ffmpeg with VideoToolbox |
| ML service (immich-ml-metal) | Same — only truly custom piece, already clean via MACHINE_LEARNING_URL |
| docker-compose hacks (fake IMMICH_WORKERS_EXCLUDE, bind mounts over binaries) | Minimal: IMMICH_WORKERS_INCLUDE=api + MACHINE_LEARNING_URL |

## Architecture

```
┌─────────────────────────────┐     ┌──────────────────────────────┐
│  Docker (Mac or NAS)        │     │  Native macOS                │
│                             │     │                              │
│  immich-server (API only)   │────▶│  Immich Accelerator          │
│  postgres                   │     │  ├─ Immich microservices     │
│  redis                      │     │  │  (bare metal Node.js)     │
│                             │     │  │  ├─ Sharp (M4 CPU)        │
│  IMMICH_WORKERS_INCLUDE=api │     │  │  ├─ ffmpeg (VideoToolbox) │
│  MACHINE_LEARNING_URL=...   │     │  │  └─ connects to DB/Redis  │
│                             │     │  │                            │
│                             │◀────│  └─ ML service               │
│                             │     │     (immich-ml-metal)         │
│                             │     │     ├─ CLIP (MLX/Metal GPU)  │
│                             │     │     ├─ Faces (Vision/ANE)    │
│                             │     │     └─ OCR (Vision/ANE)      │
└─────────────────────────────┘     └──────────────────────────────┘
```

## User experience

### First run (setup wizard)

1. "Where's your Immich?"
   - Scan for local Docker containers (check for immich_server)
   - Or enter NAS IP / Docker host
2. Read container env to get DB_HOST, DB_PASS, REDIS_HOST, etc.
3. Detect running Immich version from container image tag
4. Check out matching `immich-app/immich` at that version tag
5. `npm install` in managed directory (`~/.immich-accelerator/server/v2.x.x/`)
6. Detect available hardware (VideoToolbox, Metal GPU for ML)
7. Configure Docker:
   - Set `IMMICH_WORKERS_INCLUDE=api` (disable microservices in container)
   - Set `MACHINE_LEARNING_URL=http://host:3003` (point ML to native service)
8. Start native worker + ML service

### Running (menu bar)

- Status icon: green (running), yellow (processing), red (error)
- Click to see: throughput stats, job queue status, logs
- "Pause" / "Resume" controls
- "Open Immich" link to web UI

### Auto-update

- Monitor Immich container version (poll Docker API or watch for restarts)
- When version changes:
  1. Stop native worker
  2. Check out new version tag
  3. `npm install`
  4. If install succeeds: restart worker
  5. If install fails: alert user, fall back to Docker handling everything
- ML service updates independently (our code, our releases)

### CLI mode

```bash
# Setup
immich-accelerator setup              # interactive wizard
immich-accelerator setup --docker-host 10.0.0.14

# Run
immich-accelerator start              # start worker + ML
immich-accelerator stop
immich-accelerator status

# Update
immich-accelerator update             # check and update to match Immich version
```

## What the Immich maintainer sees

- People running Immich's code, unmodified
- Standard microservices replica architecture
- ML is a separate service with matching API (already blessed)
- No PRs to Immich needed
- Bugs from bare-metal setups handled by us, not filed on Immich

## What we need to validate

- [ ] Can Immich's server run on macOS with `npm install`? (native deps: Sharp, bcrypt, etc.)
- [ ] Does system ffmpeg get used when running bare metal? Or does Immich hardcode a path?
- [ ] Does VideoToolbox "just work" when Immich calls system ffmpeg?
- [ ] What's the actual performance difference for video transcoding (VideoToolbox vs Docker CPU)?
- [ ] Does `IMMICH_WORKERS_INCLUDE=api` fully disable microservices in the container?
- [ ] Can the native worker connect to Docker's Postgres/Redis from the host?
- [ ] How does file access work? The worker needs read access to uploaded photos and write access to thumbnails/encoded video.

## Tech stack

- **App shell:** Swift (menu bar item) or Electron/Tauri (cross-platform potential, though macOS-only for now)
- **CLI:** Python or Node.js (Node might be simpler since we're already running Node for Immich)
- **ML service:** Python (existing immich-ml-metal, unchanged)
- **Immich worker:** Node.js (Immich's own code, unmodified)

## Risks

1. **Native deps may not build on macOS.** Sharp uses libvips which does build on macOS (Homebrew). But other Immich deps might assume Linux.
2. **File paths.** Docker containers see different paths than the host. The native worker needs to see the same files at the same paths, or Immich needs path translation. This was the problem our current project solves with UPLOAD_DIR/PHOTOS_DIR.
3. **Breaking changes.** When Immich updates, new deps or new worker behavior could break our setup. Auto-update mitigates but doesn't eliminate this.
4. **Support burden.** If users file bugs on Immich from bare-metal setups, the maintainers won't be happy. We need clear messaging that bare-metal issues come to us.

## Phased approach

### Phase 1: Validate (this week)
- Try running Immich's server natively on the Mac Mini
- `git clone`, `npm install`, see what breaks
- Connect to Docker's Postgres/Redis
- Run a microservices worker, process some photos
- Measure: does VideoToolbox work? How fast are thumbnails?

### Phase 2: CLI tool
- Automate the setup (detect Immich, checkout code, install, configure)
- `immich-accelerator setup && immich-accelerator start`
- Version matching and auto-update

### Phase 3: Menu bar app
- Swift menu bar item wrapping the CLI
- Status, logs, controls
- Setup wizard GUI

### Phase 4: Polish
- launchd integration (start on boot)
- Error recovery (Immich restart, network issues)
- Telemetry/stats (photos processed, time saved)
