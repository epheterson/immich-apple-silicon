# E2E real-execution extension

## Why

The v1.4.0 → v1.4.3 cycle shipped FIVE runtime-only bugs that the VM
E2E didn't catch: #17 (dashboard uvicorn), #18 (corePlugin), #19
(path probe false-positive), #20 (ORJSONResponse crash), #24
(NODE_OPTIONS quoting). Every one of them fired only when real code
executed — `spawn` with real env vars, real HTTP renders, real
subprocess start-ups. The v1.4.2 E2E verified imports and config
validation and called that "end to end," but never actually ran the
worker with NODE_OPTIONS set or hit a real `/predict`.

This doc lists every runtime path that could have caught any of
those five bugs and adds coverage until every one is exercised.

## The bugs and what would have caught them

| Issue | Root cause class | Would have been caught by |
|---|---|---|
| **#17** dashboard uvicorn | Lazy import at runtime, no global deps | Fresh venv + dashboard.create_app() — ✅ already covered |
| **#18** missing corePlugin | OCI layer extraction shortcut | Real ghcr.io download + manifest.json check — ✅ already covered |
| **#19** path probe false positive | `/api/libraries` returns external only | Probe called against live Immich with external libs — ✅ already covered via live Immich |
| **#20** ORJSONResponse crash | `render()` asserts orjson, dep was dropped | **Real `/predict` call against the real ml/src/main.py** — ❌ not covered |
| **#24** NODE_OPTIONS quoting | Node's tokenizer doesn't honor shell quotes | **Real `immich-accelerator start` with real env vars** — ❌ not covered |

The existing E2E covers 3/5. The new steps below cover the remaining 2 plus the structural gap that let them through.

## New coverage — organized by what actually runs

### Step 9: Worker start survives for 10+ seconds

**Catches**: any bug that crashes the node worker during init, including NODE_OPTIONS errors, missing server_dir files, env-var validation failures, plugin path resolution, bullmq connection errors.

Write a valid config.json pointing at the host's Immich (via socat), then run `immich-accelerator start`. Poll the worker PID every second for 10 seconds. A successful v1.4.3 fix means the node process stays alive. Any regression of the #24 class — or any brand-new NODE_OPTIONS or env-var error — means the worker exits within a few seconds and the test fails.

### Step 10: Worker log contains the pg_dump interpose signal

**Catches**: #24 class specifically (the shim must LOAD, not just be installed). Also catches future shim breakage.

The pg_dump_shim.js writes a recognizable stderr line on load: `[immich-accelerator] postgres client interpose: …`. Tail the worker log and assert that line appears, OR send a dummy pg_dump spawn through node directly and assert the rewrite happens. Picking the direct variant because it doesn't depend on Immich's backup service actually running a backup.

### Step 11: ML service starts in STUB_MODE and answers /ping + /health

**Catches**: top-level import errors in ml/src, FastAPI app construction errors, port binding failures. Would NOT have caught #20 by itself because `/ping` doesn't render JSON, but catches any structural ML service regression.

`STUB_MODE=true python -m uvicorn src.main:app --port 3003 --app-dir ml &` inside the VM. Wait for /ping. Curl /health.

### Step 12: ML service /predict in STUB_MODE returns JSON

**Catches**: #20 class — any runtime rendering bug in the response pipeline. Also catches pydantic validation errors in PredictResponse, ORJSONResponse reintroduction, fastapi.responses bugs.

Send a multipart /predict with a synthetic 10×10 JPEG in STUB_MODE. The service returns fake data without loading real models, but the response pipeline still renders JSON through the same code path. If anyone reintroduces `ORJSONResponse` without orjson, the assertion fires at render time and this test fails.

### Step 13: ml-test CLI passes against the real stubbed service

**Catches**: any disconnect between the `ml-test` subcommand and the real ML service shape. Would have caught my wire-format bug (CLIP embedding returned as `str(embedding.tolist())`).

`immich-accelerator ml-test` with IMMICH_ML_HOST overridden to the stub. Expect 4/4 pass.

### Step 14: Worker stop + restart is clean

**Catches**: lingering PID files, socket leaks, zombie processes. Not directly a shipped bug but a long-standing class of service-management pain.

`immich-accelerator stop && start`. Verify second start also survives 10s.

## Out of scope (not testable without real models/assets)

- Real CLIP embeddings (requires mlx, ~1GB)
- Real face detection (requires Vision framework inside the VM — available on macOS but needs pyobjc)
- Real OCR (same)
- Real database backups (requires a non-trivial schema and pg_dump hitting a real DB)
- Real thumbnail generation (requires Sharp + libvips + real image files)

We deliberately skip these. They cost 30+ minutes per E2E run and duplicate tests that already run on Eric's machine as the real dev workflow.

## Bootstrap changes

The bootstrap VM needs a few more pip packages to support running `ml/src/main.py` in STUB_MODE:

- `numpy` — top-level import in main.py
- `Pillow` — top-level import in main.py
- `python-multipart` — required by FastAPI for the multipart /predict endpoint
- `pydantic` — comes transitively with fastapi

Current bootstrap has `fastapi + uvicorn[standard]`. Adding the above costs ~30 seconds and ~40MB on bootstrap. One-time.

## Expected failure modes during implementation

Starting the worker requires the server to have a valid `server_dir` structure, real node, real Sharp binding, and live DB/Redis. The VM E2E already downloads the Immich server (step 3). DB/Redis reach the host via socat (existing). Node is installed in bootstrap. Sharp rebuild happens in step 3. Everything is in place.

The ML service needs at minimum the ml submodule checked into the VM source tarball. It's already copied in via rsync in step 0.

The only new bootstrap dep is the pip list above.

## Deliverables

1. `scripts/e2e-bootstrap-vm.sh` — add numpy/Pillow/python-multipart to pip install
2. `scripts/e2e-fresh-install.sh` — steps 9–14
3. `docs/testing/e2e-vm.md` — update runbook section
4. Rebuild the bootstrap base VM once
5. Run full E2E, iterate until all steps green
6. Commit each working step, PR when done
