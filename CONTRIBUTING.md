# Contributing

New to the repo? See [Repo layout](README.md#repo-layout) for a brief tour of each top-level directory.

## Before you push

```bash
pytest -v -m "not slow"
```

All tests must pass. CI runs the same suite, and a PR that fails there will not be merged.

One caveat while we finish fixing it: parts of the suite still use the real `~/.immich-accelerator` directory, so running it on a Mac that is currently serving a library can stop that machine's worker. Use a Mac that is not running the accelerator, or expect to restart it afterwards.

## What to test

- `pytest` covers compose template validation, regex patterns, config parsing, and fresh-install regressions
- If your change touches the ffmpeg wrapper, dashboard, or worker startup, test on a real Mac with the accelerator running
- The native Swift ML engine is in this repo under `native-ml/`. The Python engine is the `ml` submodule, which lives in [epheterson/immich-ml-metal](https://github.com/epheterson/immich-ml-metal)
- **Any change to the mlx pin, the `ml` submodule, or `native-ml/`** must also pass the real-model preflight gate (`scripts/ml-preflight.py` / `scripts/native-ml-preflight.py`) on real Apple Silicon. A `STUB_MODE` test is not sufficient, and has twice let a crashing build through. See [scripts/README.md](scripts/README.md) for why

## PR guidelines

- One concern per PR
- Keep diffs small. Under 200 lines is ideal
- You do not need to touch `CHANGELOG.md`. The release PR writes it and credits you there, which avoids everyone conflicting on the same file
- Describe what the end state is, not the history of how you got there

## Reporting a bug

A report that someone can reproduce is worth as much as a patch, and several of the fixes in this project came from one. Useful things to include: your macOS and Mac model, the accelerator version, your Immich version, whether the library is local or on a network share, and the relevant part of `immich-accelerator logs`.
