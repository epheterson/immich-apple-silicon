# Changelog

## 0.1.7 — 2026-03-29

### Thumbnail Worker
- Batch DB writes: single commit per batch instead of per asset (80→4 RTTs for batch of 20)
- NFS read-ahead: prefetch next asset's source file in background thread
- Thumbhash computed from temp JPEG already in memory (eliminates extra file read)

## 0.1.6 — 2026-03-29

### ML Service (upstream PR [sebastianfredette/immich-ml-metal#4](https://github.com/sebastianfredette/immich-ml-metal/pull/4))
- Concurrent inference: CLIP (GPU), face detection (ANE), face embedding (CPU/CoreML), OCR (ANE) run simultaneously via `asyncio.gather`
- Batched face embeddings: single ONNX call for N faces instead of N separate calls
- In-memory CLIP: eliminated temp file I/O, PIL image passed directly to MLX processor
- Inference locks narrowed: image decode/preprocessing outside GPU lock
- Dedicated thread pool, per-task timing logs, busy guards for model unloading
- **3.2x faster** on group photos (5 faces: 1899ms → 599ms)

## 0.1.5 — 2026-03-29

- Configurable Docker container paths (`CONTAINER_UPLOAD_PATH`, `CONTAINER_PHOTOS_PATH`) for setups where Immich runs on a separate machine (e.g., Synology NAS) with non-standard volume mounts
- Supports split deployments: Immich server on NAS, native services on Mac

## 0.1.4 — 2026-03-25

- Auto-suppress Spotlight indexing on generated thumbnail/video directories
- README: documented all integrated performance mitigations per component
- Integration test skips gracefully when no pending assets
- Removed personal path from test docstring

## 0.1.3 — 2026-03-24

### Fixed (from code review)
- F_NOCACHE file descriptor leak: fd now closed in `finally` block
- vm_stat page size parsed from output instead of hardcoded (supports Intel Macs)
- Batch failure backoff uses separate counter (per-asset errors no longer jump to max backoff)
- Test fixture preserves `_stats["since"]` to match production reset behavior
- README: corrected `docker compose down` wording (volumes need `-v` flag to be deleted)

## 0.1.1 — 2026-03-24

- FFmpeg proxy handles concurrent requests (was single-threaded, blocking parallel transcodes)
- Thread-safe stats tracking
- README: recommended Immich transcode settings (accept HEVC, optimal policy — eliminates 80-90% of unnecessary re-encoding)

## 0.1.0 — 2026-03-23

Initial release. GPU-accelerated Immich on Apple Silicon.

### Thumbnail Worker
- Core Image (Metal GPU) thumbnail generation — 5x faster than Docker Sharp
- Single-pass `generate_all()`: one NFS read → two GPU scales → thumbhash
- 318/min sustained on NFS, ~830/min theoretical on local SSD, 30% CPU
- Self-healing: survives Immich restarts/updates with exponential backoff
- Persistent DB connection, UPSERT-safe writes to Immich's database
- 25 automated tests (resize, thumbhash, DB, integration, generate_all)

### FFmpeg Proxy
- VideoToolbox hardware video encoding (`h264`/`hevc` → `h264_videotoolbox`/`hevc_videotoolbox`)
- Hardware video decoding (`-hwaccel videotoolbox` injection)
- WebP output fallback via PIL (Homebrew ffmpeg typically lacks libwebp)
- Container path → host path translation
- Graceful fallback to container ffmpeg if proxy unreachable
- 51 automated tests (encoder remap, hwaccel injection, WebP detection, edge cases)

### ML Service
- Maintained fork of [immich-ml-metal](https://github.com/sebastianfredette/immich-ml-metal) included as git submodule
- Apple Vision (face detection) + MLX (CLIP) + CoreML (face recognition)
- 21x faster than Docker ML

### Infrastructure
- launchd service plists for all three services
- Pre-push hook enforcing version bump + changelog entry
- Example docker-compose.yml with all modifications documented
- Comprehensive README: safety, reversibility, update procedures, benchmarks

## 0.1.2 — 2026-03-24

- Memory-aware thumbnail worker: backs off when RAM < 500MB
- F_NOCACHE on source image reads to prevent buffer cache bloat
- gc.collect() between batches to free CIImage/PIL objects promptly
- ML model idle unloading: CLIP and ArcFace freed after 120s inactive (~700MB)
- FFmpeg proxy stats include session timestamp + /stats/reset endpoint
- Threaded proxy for parallel video transcodes
- README: SMB required (not NFS), bind mounts required (not Docker volumes), ulimits, initial import guide
