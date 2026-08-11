# Contributing

New to the repo? [ARCHITECTURE.md](ARCHITECTURE.md) has one paragraph per top-level directory.

## Before you push

```bash
pytest -v -m "not slow"
```

All tests must pass. CI runs the same suite — if it fails there, the PR won't be merged.

## What to test

- `pytest` covers compose template validation, regex patterns, config parsing, and fresh-install regressions
- If your change touches the ffmpeg wrapper, dashboard, or worker startup, test on a real Mac with the accelerator running
- ML changes go through the [upstream repo](https://github.com/sebastianfredette/immich-ml-metal)
- **Any change to the mlx pin or the `ml` submodule** must also pass the real-model preflight gate (`scripts/ml-preflight.py` / `scripts/native-ml-preflight.py`) on real Apple Silicon — see [CLAUDE.md](CLAUDE.md) and [scripts/README.md](scripts/README.md) for why a `STUB_MODE` test isn't sufficient

## PR guidelines

- One concern per PR
- Keep diffs small — under 200 lines is ideal
- Update CHANGELOG.md if user-facing
