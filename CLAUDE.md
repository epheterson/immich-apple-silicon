# Claude Code Instructions

## Git workflow

- **NEVER touch main without Eric's explicit approval.** No merging to main, no committing to main, no pushing to main. Work on branches only. When the branch is ready, say so and wait for Eric to tell you to merge and push.
- Squash merge to main. One clean commit per release.
- Version bump + CHANGELOG entry required for every push to main. Enforced by the `version-bump` CI job and `tests/test_release_hygiene.py`, not just convention: `auto-tag` publishes the CHANGELOG section verbatim as the release notes, so a missing bump ships silently and a missing heading makes the notes run on into every previous release.
- Contributor PRs from forks are exempt from the bump. They merge unversioned and our own release PR carries the version and the changelog entry that credits them.
- **The release is fully automated. After a merge to main, do nothing but watch.** `auto-tag` in `ci.yml` reads VERSION, creates and pushes `vX.Y.Z`, publishes that version's CHANGELOG section as the release notes, and triggers `update-homebrew.yml`, which builds the native ML bundle and menu-bar app, attaches them to the release, and pushes the formula and cask to epheterson/homebrew-immich-accelerator.
- **Never create or push a tag by hand, and never trigger the Homebrew workflow by hand.** `auto-tag` skips when the tag already exists, and both its release step and its Homebrew trigger are gated on having created it. A manual tag therefore makes the whole release a silent no-op that still reports success. To recover: delete the tag (`git push origin :refs/tags/vX.Y.Z`, `git tag -d vX.Y.Z`) and re-run the `auto-tag` job.
- Watch `gh run list --workflow=ci.yml --branch=main`, then the update-homebrew run. Do not hand-edit the tap. Verify on the Mac Mini once the tap has moved.
## Code style

- Python: type hints, f-strings, pathlib for paths.
- Keep it simple. No abstractions for one-time operations.
- The ffmpeg wrapper is bash. Keep it minimal, no unnecessary forks.

## Testing

- Deploy to Mac Mini (`ssh macmini`) and verify before claiming anything works.
- Use Playwright for dashboard screenshots.
- Check processing progress via the Immich API, not assumptions.

### mlx / ML changes require the real-model preflight gate (NON-NEGOTIABLE)

Any change to the mlx pin or the `ml` submodule MUST pass `scripts/ml-preflight.py` on real Apple Silicon before merge. The gate boots the actual ML service with `STUB_MODE=false` and hammers `/predict` with real concurrent CLIP inference, then detects the SIGABRT.

A green unit test, a `STUB_MODE=true` test (the fork's own `test_predict.py`), or a bare `mlx_clip.image_encoder` loop is **NOT** sufficient and does **NOT** count as validation. mlx 0.31.2 (gpu-stream) and 0.32.0 (cpu-stream) both pass those proxies yet hard-crash the real `/predict` service (#38, #103). Proxy validation is exactly how a bad pin shipped once already. Run the real gate, or do not ship the change.

## Immich compatibility

- Use jellyfin-ffmpeg (same as Docker). Don't patch Homebrew ffmpeg.
- The goal is identical output to Docker Immich wherever possible.
- Document every deviation in the "Known differences" README table.
