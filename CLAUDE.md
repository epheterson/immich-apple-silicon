# Claude Code Instructions

## Git workflow

- **NEVER touch main without Eric's explicit approval.** No merging to main, no committing to main, no pushing to main. Work on branches only. When the branch is ready, say so and wait for Eric to tell you to merge and push.
- Squash merge to main. One clean commit per release.
- Version bump + CHANGELOG entry required for every push to main.
- Tag releases as `vX.Y.Z` matching the VERSION file.

## Code style

- Python: type hints, f-strings, pathlib for paths.
- Keep it simple. No abstractions for one-time operations.
- The ffmpeg wrapper is bash — keep it minimal, no unnecessary forks.

## Testing

- Deploy to Mac Mini (`ssh macmini`) and verify before claiming anything works.
- Use Playwright for dashboard screenshots.
- Check processing progress via the Immich API, not assumptions.

## Immich compatibility

- Use jellyfin-ffmpeg (same as Docker). Don't patch Homebrew ffmpeg.
- The goal is identical output to Docker Immich wherever possible.
- Document every deviation in the "Known differences" README table.
