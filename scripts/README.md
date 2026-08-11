# scripts/

Everything here supports building, testing, and releasing the accelerator — none of it ships to users. Grouped by purpose (the files themselves are flat in this directory; see below for the constraints on splitting them into subdirectories).

## Release

- **`render-formula.sh`** — single source of truth for the Homebrew formula. Used by both the release workflow and the CI formula-check job, so the formula is validated on every PR instead of first being exercised when a user installs it.

## Real-model ML preflight gates

These are the gate [CLAUDE.md](../CLAUDE.md) calls **non-negotiable** for any change to the mlx pin or the `ml` submodule: they boot the actual ML service with real models (`STUB_MODE=false`) and hammer `/predict` with concurrent inference, which is the only way that has ever caught the mlx SIGABRT regressions (#38, #103) that unit tests and `STUB_MODE` tests miss.

- **`ml-preflight.py`** — gates the Python `ml` submodule service.
- **`native-ml-preflight.py`** — the equivalent gate for the native Swift engine.
- **`native-ml-siglip-benchmark.py`** — not a gate; benchmarks the SigLIP/SigLIP2 mlx path referenced from [docs/ml-engine.md](../docs/ml-engine.md).

## End-to-end VM harness

Runs a full fresh-install flow inside a disposable Tart VM against an isolated Immich stack — see [docs/testing/e2e-vm.md](../docs/testing/e2e-vm.md) for the full walkthrough. Typical order: `e2e-stack.sh up` (once) → `e2e-bootstrap-vm.sh` (once) → `e2e-host-portforward.sh start` + `e2e-run.sh` (repeatable) → `tart-cleanup.sh --all` (when done).

- **`e2e-stack.sh`** (+ **`e2e-stack.yml`**) — lifecycle and API-key bootstrap for the isolated Immich stack (Postgres/Redis/API-only server on port-shifted loopback addresses) that the VM tests against. Never points at a developer's real Immich instance.
- **`e2e-bootstrap-vm.sh`** — one-time: clones the macOS base image, installs Homebrew + python@3.11 + git, snapshots it as the reusable baseline every per-run clone starts from (~10 min saved per run).
- **`e2e-host-portforward.sh`** — ephemeral `socat` forwarders so the VM (which can't reach host-loopback services directly) can reach the isolated stack.
- **`e2e-run.sh`** — clones the bootstrapped VM, runs `e2e-fresh-install.sh` inside it, tears the clone down on success *and* failure.
- **`e2e-fresh-install.sh`** — the actual test script that runs inside the VM: real `node`/Sharp compatibility checks, starts ML in `STUB_MODE`, starts the real worker, exercises the dashboard and CLI subcommands against it.
- **`tart-cleanup.sh`** — kills/deletes `immich-*` Tart VMs; `--all` also frees the ~30GB cached base image.

## Constraints on reorganizing this further

Every script above is referenced by relative path (`scripts/foo.sh`) from CI workflows (`.github/workflows/ci.yml`, `update-homebrew.yml`, `native-bundle.yml`, `upstream-sync-check.yml`), from [CLAUDE.md](../CLAUDE.md)'s preflight-gate requirement, and from the scripts' own cross-references to each other. Splitting this directory into subdirectories (e.g. `scripts/e2e/`, `scripts/release/`, `scripts/preflight/`) is tracked as a possible follow-up but requires updating every one of those call sites in lockstep — see `docs/plans/2026-08-09-repo-reorg-roadmap.md` (local, gitignored) for the status of that follow-up.
