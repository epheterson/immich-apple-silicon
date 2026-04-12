# Fresh-install E2E testing via tart Mac VM

## Why this exists

Two production bugs (#17, #18) shipped in v1.4.0 because every pre-release
check was run on the maintainer's machine, which had globally-installed
Python packages and leftover state that masked both regressions. The
reporter hit both within 14 minutes of first trying a fresh install.

The unit tests in `tests/test_fresh_install.py` close the specific holes,
and the `fresh-install-macos` CI job runs them on clean GitHub runners.
Neither covers the *full* install flow: `brew install` → post_install venv
build → `immich-accelerator setup` → `download_immich_server` → `dashboard`
serving real data.

This doc describes the tart-based VM pipeline that does.

## Topology

```
Apple Silicon host (your Mac)
├─ Existing OrbStack containers (not touched)
│  ├─ immich_server      :2283   ← your live Immich, untouched
│  ├─ immich_postgres    :5432   bound 127.0.0.1
│  └─ immich_redis       :6379   bound 127.0.0.1
│
├─ tart (Apple Virtualization.framework)
│  └─ immich-test VM (macOS Sonoma)
│     ├─ Homebrew
│     ├─ python@3.11
│     └─ Our formula under test
│
└─ Host port forwarders (socat)
   └─ Expose the host's 127.0.0.1 services on 192.168.64.1 so
      the VM bridge can reach them. Ephemeral, test-scoped.
```

## Phases

### 1. Bootstrap (one-time, ~15 min)

- `tart pull ghcr.io/cirruslabs/macos-sonoma-base:latest` (~30 GB)
- `tart clone macos-sonoma-base immich-test-base`
- Boot VM, install Homebrew, python@3.11, git, vips, node, libpq
- Pre-install `fastapi` + `uvicorn[standard]` so per-run tests don't hit PyPI
- `tart stop immich-test-base`
- Peak disk: ~60 GB (base + clone)

### 2. Test run (per-PR, ~2-3 min)

- `tart clone immich-test-base immich-test-run-$TIMESTAMP`
- `tart run --no-graphics immich-test-run-$TIMESTAMP &`
- Install a throwaway ssh key (one-time password auth via sshpass)
- Rsync the source under test into the VM
- Run `scripts/e2e-fresh-install.sh` inside the VM
- Tear down the VM unconditionally (success or fail)
- Peak extra disk during run: ~5 GB

### 3. Tear-down (always — cleanup rule)

- All test-run VMs: deleted after every run (success or fail)
- `immich-test-base` (bootstrap VM): kept between runs for speed
- Cached OCI image: kept between runs
- **Nuke-all command**: `scripts/tart-cleanup.sh --all` — deletes every
  VM in the `immich-*` namespace AND the cached `macos-sonoma-base`
  image. Frees ~65 GB in under 30 seconds.

## What the E2E test verifies

End-to-end, inside the fresh VM:

1. **Python + dashboard deps importable** (issue #17 class) — `fastapi` and
   `uvicorn[standard]` resolve in the system python pre-installed at
   bootstrap, matching what the Homebrew formula's ml venv ships.
2. **`dashboard.create_app()` loads** — exact reproduction of the #17
   ModuleNotFoundError fix, driven against the branch's source.
3. **`download_immich_server` extracts corePlugin** (issue #18 class) —
   real ghcr.io pull of an Immich image, verifying `build-data/corePlugin/manifest.json`
   exists.
4. **`dashboard` subcommand serves HTTP 200 at `/`** — real launch of
   the dashboard with a config pointing at the live Immich on the host.
5. **`/api/status` returns JSON with version field** — proves the
   dashboard can talk to Postgres/Redis through the socat bridge.
6. **`_detect_docker_media_prefix` resolves the library root** (issue #19) —
   calls `/api/libraries.importPaths[0]` against the live Immich.
7. **`cmd_start` refuses to start with mismatched `upload_mount`** —
   proves the #19 guard works end-to-end.
8. **`ml-test` subcommand is wired up** (issue #20) — runs the
   diagnostic and expects the proper failure shape when the ML service
   isn't reachable inside the VM.

## Host-to-VM networking

Immich's postgres and redis are bound to 127.0.0.1 for security. The
VM cannot reach 127.0.0.1-bound host services directly.

Approach: bring up ephemeral socat listeners on `192.168.64.1` (the
tart Shared-mode bridge IP) during the test run, tear them down after.
`scripts/e2e-host-portforward.sh` owns this — starts socat with a
known pidfile, `trap EXIT` kills them.

Bindings (ephemeral, test-scoped):
- `192.168.64.1:12283 → 127.0.0.1:2283` (Immich HTTP)
- `192.168.64.1:15432 → 127.0.0.1:5432` (Postgres)
- `192.168.64.1:16379 → 127.0.0.1:6379` (Redis)

These are only up while a test is running. They don't modify your
running docker-compose or re-bind any existing ports.

## SSH into the VM

sshpass is fragile when ssh's stdin is a pipe (rsync, tar|ssh), because
sshpass creates a pty for ssh's prompt and piped bytes never reach
ssh. The scripts work around this by using sshpass **once** with
password auth to install a throwaway ed25519 pubkey, then using plain
key-based ssh/rsync for every subsequent call. The key is generated
per run and deleted in the exit trap.

## Not doing

- **Not running this on every git push.** The CI `fresh-install-macos`
  job is the per-push gate (fast, ~1-2 min). The tart VM E2E is pre-
  release only, triggered manually before tagging.
- **Not running Xcode / heavy simulators** in the VM. The
  `macos-sonoma-base` image is ~30 GB. The `macos-sonoma-xcode`
  variant is ~90 GB — we don't need it.
- **Not shipping the VM image anywhere.** It's reproducible from
  `tart pull` + `scripts/e2e-bootstrap-vm.sh`.

## Runbook

```bash
# One-time: bootstrap the base VM with Homebrew + python + deps
scripts/e2e-bootstrap-vm.sh

# Per-PR: run E2E against the current branch
scripts/e2e-run.sh

# After tests: free all disk (drops base VM and cached image)
scripts/tart-cleanup.sh --all
```
