# Contributing

New to the repo? See [Repo layout](README.md#repo-layout).

## Before you push

```bash
pytest -v -m "not slow"
```

All tests must pass. CI runs the same suite.

Note: parts of the suite still use the real `~/.immich-accelerator`, so running it on a Mac that is serving a library can stop that machine's worker.

## What to test

- If your change touches the ffmpeg wrapper, dashboard, or worker startup, test it on a real Mac with the accelerator running.
- The native Swift ML engine is here, in `native-ml/`. The Python engine is the `ml` submodule ([epheterson/immich-ml-metal](https://github.com/epheterson/immich-ml-metal)).
- **Any change to `native-ml/`, the `ml` submodule, or the mlx pin** must pass the preflight gate (`scripts/ml-preflight.py`, `scripts/native-ml-preflight.py`) on real Apple Silicon. A `STUB_MODE` test is not enough, and has twice let a crashing build through.

## PRs

- One concern per PR, and keep it small.
- Skip `CHANGELOG.md`. The release PR writes it and credits you.
- Describe the end state, not how you got there.

## Bug reports

A report someone can reproduce is worth as much as a patch. Include your Mac and macOS version, the accelerator and Immich versions, whether the library is local or on a network share, and the relevant logs.
