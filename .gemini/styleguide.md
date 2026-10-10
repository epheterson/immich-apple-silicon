# Review guide

This project runs Immich's microservices natively on macOS (Apple Silicon) instead of in Docker.

- The goal is output identical to Docker Immich. Flag any change that would make thumbnails, metadata, transcodes, backups or ML results differ from Docker, unless it is listed in the README's "Known differences" table.
- Never block or slow the worker's start. Slow checks belong in a background process nobody waits on.
- `immich_accelerator/hooks/` holds Node `--require` shims loaded into Immich's worker. Immich 3.3+ is ESM, so interception must work through `module.registerHooks` as well as `Module._load`. A shim that cannot apply must warn and degrade, never crash the worker.
- GitHub Actions are pinned to full commit SHAs with least-privilege permissions. Scheduled jobs do heavy work only when an input changed.
- Releases are automated from VERSION on merge to main; every release PR bumps VERSION and adds a CHANGELOG entry.
- Python: type hints, f-strings, pathlib. No abstractions for one-time operations. The ffmpeg wrapper is bash and stays minimal.
