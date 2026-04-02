# PLAN — Immich Accelerator

## Status: Library processing (309k assets)
Running on M4 Mac Mini. ML stable with gpu_lock fix. All queues active.

## Track 1: ML upstream (sebastianfredette/immich-ml-metal)

- [x] Pull upstream commit `f879301` (invalid face model packs)
- [x] Address Sebastian's PR #4 feedback (error propagation, lock safety, config cleanup)
- [x] Push fixes, reply to PR
- [x] Metal concurrency fix — gpu_lock with forced eval (on metal-concurrency-fix branch)
- [ ] Merge metal-concurrency-fix to main (after PR #4 is resolved)
- [ ] Track MLX thread safety bug (ml-explore/mlx#3078, #2133) — remove gpu_lock when fixed

## Track 2: Accelerator

- [x] Container extract approach (no source build)
- [x] Sharp darwin binary + libvips for HEIF support
- [x] IMMICH_MEDIA_LOCATION for path mapping
- [x] Auto-update on version change
- [x] Atomic config, PID reuse detection
- [x] Auto-install @img/sharp-libvips-darwin-arm64 during setup
- [ ] Handle VideoToolbox ffmpeg (Immich doesn't support it as accel option)

## Track 3: Documentation

- [x] README rewrite
- [x] CHANGELOG v1.0.0
- [x] Migration section
- [ ] Document concurrency tuning recommendations
- [ ] Document known limitations (VideoToolbox, corePlugin)

## Track 4: Validation

- [x] Fresh DB from clean state
- [x] Worker + ML stable under load
- [x] HEIF/HEIC support working
- [ ] Full library processing complete
- [ ] Search working (CLIP embeddings)
- [ ] Face recognition grouping working
- [ ] Video playback working

## Processing rates (M4 24GB, 309k library)

| Task | Rate/min | Hardware | Lock |
|------|----------|----------|------|
| Thumbnails | ~62 | CPU (Sharp/libvips NEON) | none |
| CLIP | ~150 | Metal GPU (MLX) | gpu_lock |
| Face detect | ~79 | ANE (Vision) | none |
| Face embed | ~79 | CPU (ONNX CoreML) | none |
| OCR | ~113 | ANE (Vision) | none |

At these rates: ~5h thumbnails, ~34h CLIP for full library.

## Known issues

1. **corePlugin WASM error** — non-fatal, container extract hardcodes /build path
2. **MLX not thread-safe** — CLIP serialized via gpu_lock (ml-explore/mlx#3078)
3. **No VideoToolbox in Immich** — accel options: nvenc/qsv/vaapi/rkmpp only
4. **Sharp needs extra packages** — @img/sharp-darwin-arm64 + @img/sharp-libvips-darwin-arm64
5. **Thumbnail rate lower than old Core Image approach** — CPU-bound Sharp vs GPU Core Image. Research confirms GPU won't help (bottleneck is decode/encode, not resize)

## For Immich discussion (mertalev)

Things to bring up:
- VideoToolbox as an accel option for Apple Silicon users
- Our approach works: their code, bare metal, supported microservices replica architecture
- What would a "THUMBNAIL_URL" or generic compute offloading API look like?
- Minimum ask: just add `videotoolbox` to the accel enum
