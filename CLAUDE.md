# Claude Code Instructions

## Git workflow

- **NEVER touch main without Eric's explicit approval.** No merging to main, no committing to main, no pushing to main. Work on branches only. When the branch is ready, say so and wait for Eric to tell you to merge and push.
- Squash merge to main. One clean commit per release.
- Version bump + CHANGELOG entry required for every push to main.
- Tag releases as `vX.Y.Z` matching the VERSION file.
- **After every tag push**: update the Homebrew tap (epheterson/homebrew-immich-accelerator) via `gh api` — new version, tarball URL, sha256. Verify on Mac Mini. Never skip this.

## Code style

- Python: type hints, f-strings, pathlib for paths.
- Keep it simple. No abstractions for one-time operations.
- The ffmpeg wrapper is bash — keep it minimal, no unnecessary forks.

## Testing

- Deploy to Mac Mini (`ssh macmini`) and verify before claiming anything works.
- Use Playwright for dashboard screenshots.
- Check processing progress via the Immich API, not assumptions.

### mlx / ML changes require the real-model preflight gate (NON-NEGOTIABLE)

Any change to the mlx pin or the `ml` submodule MUST pass `scripts/ml-preflight.py` on real Apple Silicon before merge. The gate boots the actual ML service with `STUB_MODE=false` and hammers `/predict` with real concurrent CLIP inference, then detects the SIGABRT.

A green unit test, a `STUB_MODE=true` test (the fork's own `test_predict.py`), or a bare `mlx_clip.image_encoder` loop is **NOT** sufficient and does **NOT** count as validation. mlx 0.31.2 (gpu-stream) and 0.32.0 (cpu-stream) both pass those proxies yet hard-crash the real `/predict` service (#38, #103). Proxy validation is exactly what shipped the v1.5.29 regression that broke users' ML. Run the real gate, or do not ship the change.

## Immich compatibility

- Use jellyfin-ffmpeg (same as Docker). Don't patch Homebrew ffmpeg.
- The goal is identical output to Docker Immich wherever possible.
- Document every deviation in the "Known differences" README table.
