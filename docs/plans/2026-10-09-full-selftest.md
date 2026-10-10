# Full self-test (1.17.11)

Goal: every time Immich or the accelerator changes version, prove that every kind of work the accelerator does still produces the expected output, and say exactly what broke if not. 1.17.10's self-test proves the shims are reached; this one runs the jobs.

## Principle

Drive Immich's own code wherever it exists, with tiny fixtures, so an upstream change to how Immich does the work is caught, not just a change to how it loads modules. Where the self-test cannot reach Immich's code (it moved), report a harness error: a warning locally, a failure in the CI canary.

## Checks

| Check | How | Expected |
|---|---|---|
| `jpeg_thumbnail` | `MediaRepository.decodeImage` on a JPEG | decoded size |
| `heic_thumbnail` | same, HEVC HEIC (exists) | decoded size |
| `sharp_native` | exists | no `emscripten` |
| `metadata` | `MetadataRepository.readTags` on a JPEG with EXIF | `DateTimeOriginal`, `Make` match the fixture |
| `video_probe` | `MediaRepository.probe` on a 1 s HDR HEVC clip with audio | one video and one audio stream, 10-bit |
| `video_thumbnail` | `ThumbnailConfig.create(defaults.ffmpeg).getCommand(...)` then `MediaRepository.transcode` through the ffmpeg wrapper | a decodable image at the target size |
| `video_transcode` | `BaseConfig.create(defaults.ffmpeg, []).getCommand(...)` then `transcode`, forced to transcode | a playable H.264 file, tone-mapped (SDR transfer) |
| `pg_dump` | spawn `/usr/lib/postgresql/16/bin/pg_dump --version` and `gzip --rsyncable` through the shim | Homebrew pg_dump answers; gzip output decompresses |
| `job_retry` | exists | patched |
| `ml_clip`, `ml_faces`, `ml_ocr` | `/predict` against the configured ML service after it is ready (local only) | embedding of the model's size; one face on the face fixture; text on the OCR fixture |

Camera RAW is left out: no RAW fixture is small enough to ship. Database, Redis and the library mount are already preflighted before every start and block it when broken.

## Where it runs

- `cmd_start`: node checks before the worker starts (as now), ML checks once the ML service reports ready; one combined result in `selftest.json`, shown in `status`.
- `immich-accelerator selftest`: all of it on demand; `--immich-version` downloads a server and runs everything except ML (CI).
- Canary: adds jellyfin-ffmpeg so the video checks run on GitHub's Apple Silicon runner.

## Moving parts added

Fixtures (a JPEG with EXIF, a 1 s HDR clip, a face, an OCR card); the ffmpeg wrapper setup moved into a helper both callers use; the ML checks from `ml-test` reused. No new service, no new primary-path work: everything runs once per version change, behind the existing preflight.

## Done when

- Every check passes on Immich 3.2.4 (prod) and 3.3.1 on the Mini, and in the canary.
- Each check fails with a clear message when its piece is broken (wrapper removed, shim disabled, ML stopped), verified by breaking each one on purpose.
- Tests cover the Python plumbing; release notes and docs updated.
