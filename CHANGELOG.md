# Changelog

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
