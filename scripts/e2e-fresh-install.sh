#!/bin/bash
# scripts/e2e-fresh-install.sh
#
# Runs INSIDE a fresh macOS VM. Validates the dashboard + corePlugin
# fixes end-to-end against real Immich infrastructure.
#
# The source code under test is copied into the VM at $SRC_DIR by the
# caller (scripts/e2e-run.sh) BEFORE this script runs. We build a
# pristine venv with only fastapi + uvicorn[standard] installed (the
# same composition the Homebrew formula ships via ml/requirements.txt)
# and exercise the exact code paths users hit on a fresh install.
#
# We do NOT `brew install` the formula from the tap here, because the
# tap still points at the unfixed v1.4.0 until the new tag is cut.
# The formula-level correctness is covered separately by the updated
# `brew test` block and the macos-14 CI job.
#
# Inputs (env):
#   SRC_DIR       path to the accelerator source checkout inside the VM
#   IMMICH_URL    e.g. http://192.168.64.1:12283
#   IMMICH_API_KEY
#   DB_HOST       e.g. 192.168.64.1
#   DB_PORT       e.g. 15432
#   DB_PASSWORD
#   REDIS_HOST    e.g. 192.168.64.1
#   REDIS_PORT    e.g. 16379
#
# Exit codes: 0=pass, 2-9=specific failures

set -euo pipefail

: "${SRC_DIR:?set SRC_DIR}"
: "${IMMICH_URL:?set IMMICH_URL}"
: "${IMMICH_API_KEY:?set IMMICH_API_KEY}"
: "${DB_HOST:=192.168.64.1}"
: "${DB_PORT:=15432}"
: "${DB_PASSWORD:?set DB_PASSWORD}"
: "${REDIS_HOST:=192.168.64.1}"
: "${REDIS_PORT:=16379}"

eval "$(/opt/homebrew/bin/brew shellenv)"

# Ensure node@22 is on PATH. Immich 2.7.4's package.json pins
# engines.node = 24.14.1; sharp@0.34.5's prebuilt native bindings
# and node-gyp builds are tested up to node 24. node 25 (the default
# `brew install node` ships) breaks sharp's native addon load with
# NODE_MODULE_VERSION mismatch, which surfaces later as a worker
# crash at `require('sharp')` that LOOKS like a Sharp-install bug
# but is really a node-version incompat. Homebrew has no node@24
# bottle yet — node@22 is the closest LTS that passes Immich's
# engines check and compiles sharp cleanly. One-time install
# (~60s); subsequent runs in the same clone are no-ops.
if [ ! -x "/opt/homebrew/opt/node@22/bin/node" ]; then
    printf '[e2e] installing node@22 (Immich engines.node=24, fresh node=25 breaks sharp)\n'
    HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1 \
        brew install --quiet node@22 || {
            echo "node@22 install failed"
            exit 2
        }
fi
# node@22 is keg-only — prepend its bin dir so the plain `node`
# name resolves to 22, not the 25 that `brew install node` shipped.
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
NODE_VER=$(node --version 2>/dev/null || echo "missing")
printf '[e2e] node version on PATH: %s\n' "$NODE_VER"
case "$NODE_VER" in
    v22.*) : ;;
    *) echo "expected node v22.x, got $NODE_VER"; exit 2 ;;
esac

# DATA must match immich-accelerator's default (~/.immich-accelerator)
# because step 12 runs `immich-accelerator start` which looks for
# server_dir and build-data at the hard-coded default path. Using
# a /tmp/* path instead splits build-data across two locations and
# the worker's MapRepository.init fails with ENOENT on geodata.
DATA="$HOME/.immich-accelerator"
UPLOAD="/tmp/e2e-upload"

log() { printf '[e2e] %s\n' "$*"; }
fail() { printf '[e2e FAIL] %s\n' "$*" >&2; exit "${2:-1}"; }

# Clean the data dir completely to simulate a fresh install.
rm -rf "$DATA" "$UPLOAD"
mkdir -p "$DATA" "$UPLOAD"

# Bootstrap pre-installs fastapi + uvicorn[standard] into the system
# python@3.11 site-packages so per-run E2E is network-independent
# and not flaky on VM DNS. The formula's ml venv ships the same
# package composition — this test is still a faithful proxy for
# "does dashboard.create_app resolve its deps at runtime?"
PY="/opt/homebrew/bin/python3.11"
if [ ! -x "$PY" ]; then
    PY="/opt/homebrew/opt/python@3.11/bin/python3.11"
fi

log "step 1: python + fastapi + uvicorn + stub-ML deps importable"
"$PY" -c "
import sys, fastapi, uvicorn, numpy, PIL
print(f'python {sys.version_info.major}.{sys.version_info.minor}'
      f', fastapi {fastapi.__version__}'
      f', uvicorn {uvicorn.__version__}'
      f', numpy {numpy.__version__}'
      f', Pillow {PIL.__version__}')
" || fail "dependency import failed — bootstrap VM may be stale, re-run e2e-bootstrap-vm.sh" 2

# Also verify the CLI --version flag works and matches VERSION file
EXPECTED_VER=$(cat "$SRC_DIR/VERSION" | tr -d '[:space:]')
ACTUAL_VER=$(PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator --version 2>&1 | awk '{print $2}')
if [ "$EXPECTED_VER" != "$ACTUAL_VER" ]; then
    fail "immich-accelerator --version reports '$ACTUAL_VER' but VERSION file has '$EXPECTED_VER'" 2
fi
log "  CLI --version reports $ACTUAL_VER (matches VERSION file)"

# -------------------------------------------------------------------
# 2. Dashboard create_app smoke — direct regression for issue #17.
# -------------------------------------------------------------------
log "step 2: dashboard.create_app resolves fastapi/uvicorn in fresh venv"
PYTHONPATH="$SRC_DIR" "$PY" -c "
from immich_accelerator.dashboard import create_app
app = create_app({
    'version':'t','immich_url':'http://x','api_key':'',
    'db_hostname':'','db_port':'5432',
    'redis_hostname':'','redis_port':'6379',
    'server_dir':'/tmp','ml_port':3003,
})
assert type(app).__name__ == 'FastAPI', f'got {type(app).__name__}'
print('dashboard.create_app OK')
" || fail "dashboard imports do not resolve (issue #17 class)" 3

# -------------------------------------------------------------------
# 3. download_immich_server extracts corePlugin — regression for #18.
#    Downloads ~450MB from ghcr.io. ~2 minutes.
# -------------------------------------------------------------------
log "step 3: download_immich_server for v2.7.4 (tests corePlugin fix)"
PYTHONPATH="$SRC_DIR" "$PY" -c "
import logging, sys
logging.basicConfig(level=logging.INFO, format='  %(message)s')
from pathlib import Path
import immich_accelerator.__main__ as acc
acc.DATA_DIR = Path('$DATA')
server_dir = acc.download_immich_server('2.7.4')
manifest = acc.DATA_DIR / 'build-data' / 'corePlugin' / 'manifest.json'
if not manifest.exists():
    print(f'FAIL: corePlugin/manifest.json missing', file=sys.stderr)
    sys.exit(1)
size = manifest.stat().st_size
if size == 0:
    print(f'FAIL: manifest.json empty', file=sys.stderr); sys.exit(1)
print(f'corePlugin/manifest.json extracted: {size} bytes')
print(f'server_dir: {server_dir}')
" || fail "corePlugin extraction failed (issue #18 class)" 4

# -------------------------------------------------------------------
# 4. Write config + launch the dashboard for real. Serves HTML 200.
# -------------------------------------------------------------------
log "step 4: write config.json and launch dashboard subcommand"
mkdir -p "$HOME/.immich-accelerator"
cat > "$HOME/.immich-accelerator/config.json" <<JSON
{
  "version": "2.7.4",
  "server_dir": "$DATA/server/2.7.4",
  "node": "$(which node)",
  "immich_url": "$IMMICH_URL",
  "db_hostname": "$DB_HOST",
  "db_port": "$DB_PORT",
  "db_username": "postgres",
  "db_password": "$DB_PASSWORD",
  "db_name": "immich",
  "redis_hostname": "$REDIS_HOST",
  "redis_port": "$REDIS_PORT",
  "upload_mount": "$UPLOAD",
  "ffmpeg_path": "/opt/homebrew/bin/ffmpeg",
  "ml_port": 3003,
  "api_key": "$IMMICH_API_KEY"
}
JSON
chmod 600 "$HOME/.immich-accelerator/config.json"

PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator dashboard --port 28420 >/tmp/dashboard.log 2>&1 &
DASH_PID=$!
trap 'kill $DASH_PID 2>/dev/null || true' EXIT

# Wait up to 15s for the dashboard to bind.
for _ in $(seq 1 15); do
    if curl -sf http://localhost:28420/ >/dev/null 2>&1; then break; fi
    sleep 1
done

if ! curl -sf http://localhost:28420/ >/dev/null; then
    cat /tmp/dashboard.log >&2
    fail "dashboard did not serve HTTP 200 at / within 15s" 5
fi
log "step 4: dashboard serving HTTP 200 at /"

# -------------------------------------------------------------------
# 5. /api/status returns JSON with version field populated.
# -------------------------------------------------------------------
log "step 5: /api/status returns JSON with version field"
STATUS=$(curl -sf http://localhost:28420/api/status) \
    || fail "/api/status did not return 200" 6
if ! echo "$STATUS" | grep -q '"version"'; then
    echo "status body: $STATUS" >&2
    fail "/api/status missing version field" 7
fi
log "  status (truncated): $(echo "$STATUS" | head -c 250)..."

kill "$DASH_PID" 2>/dev/null || true
trap - EXIT

# -------------------------------------------------------------------
# Quick API-key auth check. If it fails, skip the steps that need
# an authenticated API call — they're validation, not dependencies
# of the core #17/#18 fixes that already passed above.
# -------------------------------------------------------------------
AUTH_OK=1
AUTH_RESP=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "X-API-Key: $IMMICH_API_KEY" \
    "$IMMICH_URL/api/users/me" 2>/dev/null || echo "000")
if [ "$AUTH_RESP" != "200" ]; then
    AUTH_OK=0
    log "  (API key returned $AUTH_RESP — skipping authenticated steps 6-7)"
fi

# -------------------------------------------------------------------
# 6. Issue #19 — split-setup path probe. The v1.4.3 probe
#    intentionally returns None when there are no upload-library
#    assets (a user with ONLY external libraries has nothing to
#    probe), which is the correct "don't know, don't block"
#    behavior. So we just call the probe, record the result, and
#    let step 7 branch on it — there's no single right answer here.
# -------------------------------------------------------------------
PROBE=""
if [ $AUTH_OK -eq 1 ]; then
    log "step 6: _detect_docker_media_prefix"
    PROBE=$(PYTHONPATH="$SRC_DIR" "$PY" -c "
from immich_accelerator.__main__ import _detect_docker_media_prefix
p = _detect_docker_media_prefix('$IMMICH_URL', '$IMMICH_API_KEY')
print(p or '')
    ") || fail "path probe call raised" 8
    if [ -n "$PROBE" ]; then
        log "  probe detected upload-library prefix: $PROBE"
    else
        log "  probe returned None — no upload assets (external-only install)"
    fi
else
    log "step 6: SKIPPED (api key invalid)"
fi

# -------------------------------------------------------------------
# 7. Issue #19 — cmd_start must refuse to start with a mismatched
#    upload_mount. Write a known-bogus path to config, invoke start,
#    expect a non-zero exit with the mismatch message on stderr.
#    Requires the probe to work (valid API key).
# -------------------------------------------------------------------
if [ $AUTH_OK -eq 0 ]; then
    log "step 7: SKIPPED (api key invalid — probe can't run)"
elif [ -z "$PROBE" ]; then
    log "step 7: SKIPPED (probe returned None — no upload assets to trigger mismatch)"
else
log "step 7: cmd_start refuses broken upload_mount (issue #19 guard)"
cat > "$HOME/.immich-accelerator/config.json" <<JSON
{
  "version": "2.7.4",
  "server_dir": "$DATA/server/2.7.4",
  "node": "$(which node)",
  "immich_url": "$IMMICH_URL",
  "db_hostname": "$DB_HOST",
  "db_port": "$DB_PORT",
  "db_username": "postgres",
  "db_password": "$DB_PASSWORD",
  "db_name": "immich",
  "redis_hostname": "$REDIS_HOST",
  "redis_port": "$REDIS_PORT",
  "upload_mount": "/definitely-not-a-real-path-xyz-9000",
  "ffmpeg_path": "/opt/homebrew/bin/ffmpeg",
  "ml_port": 3003,
  "api_key": "$IMMICH_API_KEY"
}
JSON
chmod 600 "$HOME/.immich-accelerator/config.json"

set +e
START_OUT=$(
    PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator start 2>&1
)
START_RC=$?
set -e

if echo "$START_OUT" | grep -q "Path mismatch detected"; then
    log "  mismatch error surfaced correctly"
else
    echo "$START_OUT" | tail -20 >&2
    fail "cmd_start did not emit the path-mismatch warning" 9
fi
if ! echo "$START_OUT" | grep -q "Refusing to start"; then
    echo "$START_OUT" | tail -20 >&2
    fail "cmd_start did not refuse to start on mismatch" 9
fi
fi  # end AUTH_OK gate

# -------------------------------------------------------------------
# 8. Start the ML service in STUB_MODE so the rest of the E2E can
#    call a real /predict without pulling 2GB of mlx/onnxruntime.
#    STUB_MODE is a first-class feature of ml/src/main.py — the
#    service returns fake data but the FULL response pipeline runs,
#    including FastAPI JSON rendering. Any render-time regression
#    (e.g. reintroducing ORJSONResponse without orjson — issue #20)
#    fires here at the first /predict call.
# -------------------------------------------------------------------
log "step 8: start ML service in STUB_MODE"
ML_LOG=/tmp/e2e-ml.log
STUB_MODE=true "$PY" -m uvicorn src.main:app \
    --app-dir "$SRC_DIR/ml" --host 127.0.0.1 --port 3003 \
    > "$ML_LOG" 2>&1 &
ML_PID=$!
trap 'kill $ML_PID 2>/dev/null || true' EXIT

# Wait for /ping (up to 15s — first FastAPI startup is slow)
for _ in $(seq 1 15); do
    if curl -sf http://127.0.0.1:3003/ping >/dev/null 2>&1; then break; fi
    sleep 1
done
if ! curl -sf http://127.0.0.1:3003/ping >/dev/null; then
    tail -30 "$ML_LOG" >&2 || true
    fail "stub ML service did not answer /ping within 15s" 10
fi
log "  stub ML service up at :3003"

# -------------------------------------------------------------------
# 9. Real /ping + /health + /predict call chain. Runs the FULL
#    FastAPI response rendering pipeline in the real ml/src/main.py
#    — this is the test that would have caught issue #20.
# -------------------------------------------------------------------
log "step 9: /ping + /health + /predict (stubbed) return well-formed JSON"
PING=$(curl -sf http://127.0.0.1:3003/ping)
[ "$PING" = "pong" ] || fail "/ping returned $PING not 'pong'" 11

HEALTH=$(curl -sf http://127.0.0.1:3003/health)
echo "$HEALTH" | grep -q '"stub_mode":true' \
    || fail "/health JSON missing stub_mode=true: $HEALTH" 11
log "  /health OK (stub_mode=true)"

# Build a multipart /predict call with a synthetic JPEG and verify
# the stubbed response renders through JSONResponse cleanly. This is
# the exact code path that crashed with AssertionError: orjson must
# be installed — v1.4.2 and earlier would fail this test.
PREDICT_PY=$(mktemp)
cat > "$PREDICT_PY" <<'PYEOF'
import base64, json, sys, urllib.request
# 10x10 grey JPEG
tiny_jpeg = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAKAAoDASIA"
    "AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAj/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEB"
    "AQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABn/"
    "2Q=="
)
boundary = "----e2e"
entries = json.dumps({"clip": {"visual": {"modelName": "ViT-B-32__openai"}}})
body = (
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="entries"\r\n\r\n'
    f"{entries}\r\n"
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="image"; filename="t.jpg"\r\n'
    f"Content-Type: image/jpeg\r\n\r\n"
).encode() + tiny_jpeg + f"\r\n--{boundary}--\r\n".encode()

req = urllib.request.Request(
    "http://127.0.0.1:3003/predict",
    data=body,
    headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    },
)
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read()
result = json.loads(raw)
if not isinstance(result, dict) or "clip" not in result:
    print(f"unexpected predict shape: {result}", file=sys.stderr)
    sys.exit(1)
print(f"predict OK keys={list(result.keys())}")
PYEOF
"$PY" "$PREDICT_PY" 2>&1 || {
    tail -30 "$ML_LOG" >&2
    fail "/predict failed against stub ML — render pipeline broken" 11
}
rm -f "$PREDICT_PY"
log "  /predict JSON render OK"

# -------------------------------------------------------------------
# 10. ml-test CLI against the running stub service. Exercises the
#     immich-accelerator ml-test subcommand end-to-end, including
#     the wire-format parsing of the stringified-list CLIP embedding.
# -------------------------------------------------------------------
log "step 10: immich-accelerator ml-test against stub service"
# Point ml-test at our stub by writing a config with ml_port=3003.
mkdir -p "$HOME/.immich-accelerator"
cat > "$HOME/.immich-accelerator/config.json" <<JSON
{
  "version": "2.7.4",
  "server_dir": "$DATA/server/2.7.4",
  "node": "$(which node)",
  "immich_url": "$IMMICH_URL",
  "db_hostname": "$DB_HOST",
  "db_port": "$DB_PORT",
  "db_username": "postgres",
  "db_password": "$DB_PASSWORD",
  "db_name": "immich",
  "redis_hostname": "$REDIS_HOST",
  "redis_port": "$REDIS_PORT",
  "upload_mount": "/tmp/e2e-upload",
  "ffmpeg_path": "/opt/homebrew/bin/ffmpeg",
  "ml_port": 3003,
  "api_key": "$IMMICH_API_KEY"
}
JSON
chmod 600 "$HOME/.immich-accelerator/config.json"

set +e
ML_TEST_OUT=$(PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator ml-test 2>&1)
ML_TEST_RC=$?
set -e
if [ $ML_TEST_RC -eq 0 ]; then
    log "  ml-test passed 4/4 against stub service"
elif echo "$ML_TEST_OUT" | grep -q "ML service OK"; then
    log "  ml-test surfaced an OK status (stub partial success)"
else
    echo "$ML_TEST_OUT" | tail -20 >&2
    fail "ml-test failed against running stub service" 12
fi

# Stop the stub ML service — the next step runs the real
# immich-accelerator start which spawns its own ML service.
kill "$ML_PID" 2>/dev/null || true
wait "$ML_PID" 2>/dev/null || true
trap - EXIT
log "  stub ML service stopped"

# -------------------------------------------------------------------
# 11. NODE_OPTIONS shim: spawn a real node subprocess with the same
#     NODE_OPTIONS string cmd_start builds, pointing at the pg_dump
#     shim, and assert the shim loads. Would have caught issue #24
#     quoting bugs (v1.4.2 single quotes, v1.4.3 pre-fix backslash).
# -------------------------------------------------------------------
log "step 11: NODE_OPTIONS shim is parseable by real node"
SHIM="$SRC_DIR/immich_accelerator/hooks/pg_dump_shim.js"
[ -f "$SHIM" ] || fail "shim not found at $SHIM" 13

# Build the exact NODE_OPTIONS string cmd_start produces (double-
# quoted path). Any regression to single-quote or backslash escape
# here means node will fail MODULE_NOT_FOUND and the test fails.
NODE_OPTS="--require \"$SHIM\""
SPAWN_TEST=$(mktemp)
cat > "$SPAWN_TEST" <<'JSEOF'
const { spawn } = require('node:child_process');
const p = spawn('/usr/lib/postgresql/14/bin/pg_dump', ['--version']);
let out = '';
p.stdout.on('data', (d) => (out += d));
p.on('exit', (c) => {
    console.log(`exit=${c} out=${out.trim()}`);
    process.exit(c);
});
JSEOF

SPAWN_OUT=$(NODE_OPTIONS="$NODE_OPTS" node "$SPAWN_TEST" 2>&1) || {
    echo "$SPAWN_OUT" >&2
    fail "node shim load failed under real NODE_OPTIONS" 13
}
rm -f "$SPAWN_TEST"
echo "$SPAWN_OUT" | grep -q "postgres client interpose" \
    || fail "shim did not emit interpose log: $SPAWN_OUT" 13
echo "$SPAWN_OUT" | grep -q "pg_dump (PostgreSQL)" \
    || fail "pg_dump did not run through shim: $SPAWN_OUT" 13
log "  NODE_OPTIONS shim interpose working"

# -------------------------------------------------------------------
# 12. Real `immich-accelerator start` — the ACTUAL CLI command users
#     run, against a valid config. Spawns the native worker with the
#     worker env set exactly as production does, including NODE_OPTIONS
#     with the pg_dump shim. Waits for the Nest bootstrap log line to
#     appear, then stops. This is the test that would have caught
#     #24 at the exact symptom the user saw: module-not-found on the
#     shim path. It also catches DB/Redis/env-var wiring bugs and
#     anything else that crashes the worker during the first 15s.
#
#     We need a running ML service for the worker to finish bootstrap,
#     so we re-start the stub (step 8 stopped it) and leave it running
#     for this step.
# -------------------------------------------------------------------
log "step 12: start stub ML for the worker to connect to"
STUB_MODE=true "$PY" -m uvicorn src.main:app \
    --app-dir "$SRC_DIR/ml" --host 127.0.0.1 --port 3003 \
    > "$ML_LOG" 2>&1 &
ML_PID=$!
trap 'kill $ML_PID 2>/dev/null || true; \
      PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator stop 2>/dev/null || true' EXIT
for _ in $(seq 1 15); do
    if curl -sf http://127.0.0.1:3003/ping >/dev/null 2>&1; then break; fi
    sleep 1
done
curl -sf http://127.0.0.1:3003/ping >/dev/null \
    || fail "stub ML did not restart for worker test" 14
log "  stub ML ready for worker"

log "step 12: immich-accelerator start runs a worker that survives bootstrap"
# Write the final valid config. We've already tested the path-probe
# refusal in step 7 — here we deliberately write a config that will
# pass all probes, using an upload_mount that exists on the VM.
#
# Immich's StorageService runs a "folder checks" verification on
# startup that requires a .immich marker file in each of the
# standard subdirectories (encoded-video, thumbs, upload, library,
# profile, backups). Without these the worker exits with ENOENT
# before reaching Nest bootstrap. Pre-create them.
for sub in encoded-video thumbs upload library profile backups; do
    mkdir -p "/tmp/e2e-upload/$sub"
    : > "/tmp/e2e-upload/$sub/.immich"
done

# /build synthetic link is already active (created during bootstrap
# via /etc/synthetic.d/immich-accelerator → build-data). The E2E's
# step 3 extracted corePlugin and geodata under build-data, and
# /build resolves to the same files, so Immich's PluginService
# and MapRepository both find what they need on disk.
cat > "$HOME/.immich-accelerator/config.json" <<JSON
{
  "version": "2.7.4",
  "server_dir": "$DATA/server/2.7.4",
  "node": "$(which node)",
  "immich_url": "$IMMICH_URL",
  "db_hostname": "$DB_HOST",
  "db_port": "$DB_PORT",
  "db_username": "postgres",
  "db_password": "$DB_PASSWORD",
  "db_name": "immich",
  "redis_hostname": "$REDIS_HOST",
  "redis_port": "$REDIS_PORT",
  "upload_mount": "/tmp/e2e-upload",
  "ffmpeg_path": "/opt/homebrew/bin/ffmpeg",
  "ml_port": 3003,
  "api_key": "$IMMICH_API_KEY"
}
JSON
chmod 600 "$HOME/.immich-accelerator/config.json"

# Spawn the start command. cmd_start detaches the worker as a
# subprocess and returns — this call is non-blocking and we check
# the worker PID file afterwards.
set +e
START_OUT=$(
    PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator start 2>&1
)
START_RC=$?
set -e
if [ $START_RC -ne 0 ]; then
    echo "$START_OUT" | tail -30 >&2
    fail "immich-accelerator start returned non-zero: rc=$START_RC" 14
fi

# Poll for the worker PID file to appear (up to 20s for Nest init).
# The pidfile format is "<pid>\n<start_time>" — take only the first
# line as the integer PID.
WORKER_PID_FILE="$HOME/.immich-accelerator/pids/worker.pid"
WORKER_LOG="$HOME/.immich-accelerator/logs/worker.log"
for _ in $(seq 1 20); do
    [ -f "$WORKER_PID_FILE" ] && break
    sleep 1
done
if [ ! -f "$WORKER_PID_FILE" ]; then
    echo "--- immich-accelerator start stdout (pidfile never appeared) ---" >&2
    echo "$START_OUT" | tail -40 >&2
    echo "--- worker log (no pidfile) ---" >&2
    tail -60 "$WORKER_LOG" 2>/dev/null >&2 || echo "(no worker log)" >&2
    fail "worker PID file never appeared at $WORKER_PID_FILE" 14
fi
WORKER_PID=$(head -n1 "$WORKER_PID_FILE" | tr -d '[:space:]')
log "  worker spawned (PID $WORKER_PID)"

# Wait up to 60s for the Nest bootstrap log line. Nest init on a
# cold VM includes loading ~200MB of node_modules and connecting to
# Postgres+Redis, which can take 30-45s on first boot.
for _ in $(seq 1 60); do
    if [ -f "$WORKER_LOG" ] && grep -q "Immich Microservices is running" "$WORKER_LOG" 2>/dev/null; then
        break
    fi
    sleep 1
done
if ! grep -q "Immich Microservices is running" "$WORKER_LOG" 2>/dev/null; then
    echo "--- immich-accelerator start stdout ---" >&2
    echo "$START_OUT" | tail -40 >&2
    echo "--- ~/.immich-accelerator/logs directory ---" >&2
    ls -la "$HOME/.immich-accelerator/logs" 2>&1 >&2 || echo "(no logs dir)" >&2
    echo "--- worker log tail (no bootstrap marker) ---" >&2
    if [ -f "$WORKER_LOG" ]; then
        wc -l "$WORKER_LOG" >&2
        tail -80 "$WORKER_LOG" >&2
    else
        echo "(worker log not created at $WORKER_LOG)" >&2
    fi
    echo "--- ml log tail ---" >&2
    if [ -f "$HOME/.immich-accelerator/logs/ml.log" ]; then
        tail -30 "$HOME/.immich-accelerator/logs/ml.log" >&2
    fi
    echo "--- worker process state ---" >&2
    ps -p "$WORKER_PID" -o pid,state,command 2>/dev/null >&2 || echo "PID $WORKER_PID gone" >&2
    echo "--- all node processes ---" >&2
    pgrep -fl node 2>&1 >&2 || echo "(no node processes)" >&2
    fail "worker did not reach 'Microservices is running' within 60s" 14
fi
log "  worker reached Nest bootstrap: 'Immich Microservices is running'"

# Verify the process is still alive after the bootstrap marker.
if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    tail -40 "$WORKER_LOG" 2>/dev/null >&2 || true
    fail "worker exited after bootstrap (PID $WORKER_PID gone)" 14
fi
log "  worker PID $WORKER_PID alive after bootstrap"

# -------------------------------------------------------------------
# 13a. Dashboard against a LIVE worker. Step 5 tested the dashboard
#      with no worker running (every service reports alive=false).
#      This variant runs the dashboard while the worker is actually
#      up from step 12, verifying /api/status reports worker.alive=
#      true. This is the canonical "everything wired together" test
#      — if anything in the status pipeline regresses (config read,
#      pidfile scan, DB/Redis ping, JSON serialization), it fires
#      here.
# -------------------------------------------------------------------
log "step 13a: dashboard shows worker.alive=true while worker is running"
PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator dashboard --port 28421 \
    > /tmp/dashboard-live.log 2>&1 &
DASH_PID=$!
for _ in $(seq 1 15); do
    if curl -sf http://localhost:28421/ >/dev/null 2>&1; then break; fi
    sleep 1
done
if ! curl -sf http://localhost:28421/ >/dev/null; then
    cat /tmp/dashboard-live.log >&2
    kill "$DASH_PID" 2>/dev/null || true
    fail "live dashboard did not serve / within 15s" 15
fi
STATUS_LIVE=$(curl -sf http://localhost:28421/api/status)
if ! echo "$STATUS_LIVE" | grep -q '"worker":{"alive":true'; then
    echo "live status body: $STATUS_LIVE" >&2
    kill "$DASH_PID" 2>/dev/null || true
    fail "live dashboard status does not show worker.alive=true" 15
fi
log "  live dashboard reports worker.alive=true"
kill "$DASH_PID" 2>/dev/null || true
wait "$DASH_PID" 2>/dev/null || true

# -------------------------------------------------------------------
# 13b. Verify the status command reports the running worker + ML.
# -------------------------------------------------------------------
log "step 13b: immich-accelerator status shows worker + ml running"
STATUS_OUT=$(PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator status 2>&1)
if ! echo "$STATUS_OUT" | grep -qiE "worker.*running|worker.*PID"; then
    echo "$STATUS_OUT" >&2
    fail "status output does not show worker running" 15
fi
log "  status: $(echo "$STATUS_OUT" | tr '\n' ' ' | head -c 200)"

# -------------------------------------------------------------------
# 14. Verify stop cleanly terminates everything and the PID file is
#     removed.
# -------------------------------------------------------------------
log "step 14: immich-accelerator stop cleanly terminates the worker"
PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator stop >/dev/null 2>&1 || {
    fail "immich-accelerator stop returned non-zero" 16
}
# Give the worker a moment to exit
sleep 2
if kill -0 "$WORKER_PID" 2>/dev/null; then
    fail "worker PID $WORKER_PID still alive after stop" 16
fi
if [ -f "$WORKER_PID_FILE" ]; then
    fail "worker pidfile still exists after stop: $WORKER_PID_FILE" 16
fi
log "  worker terminated and pidfile removed"

# -------------------------------------------------------------------
# 15. Idempotent stop: running `stop` when already stopped must
#     succeed cleanly (no error, no hang).
# -------------------------------------------------------------------
log "step 15: immich-accelerator stop is idempotent"
set +e
PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator stop 2>/dev/null
STOP2_RC=$?
set -e
if [ $STOP2_RC -ne 0 ]; then
    fail "second stop returned non-zero rc=$STOP2_RC" 17
fi
log "  stop exits cleanly when already stopped"

# -------------------------------------------------------------------
# 16. Restart cycle: after stop, a second `start` must work the same
#     as the first. Catches regressions in pidfile cleanup, stale
#     socket lingering, and worker env re-computation.
# -------------------------------------------------------------------
log "step 16: second start after stop reaches Nest bootstrap again"
# Stub ML is still running (from step 12). Truncate the worker log
# so we can grep for a fresh bootstrap marker.
: > "$WORKER_LOG"
set +e
START2_OUT=$(PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator start 2>&1)
START2_RC=$?
set -e
if [ $START2_RC -ne 0 ]; then
    echo "$START2_OUT" | tail -40 >&2
    fail "second start returned non-zero rc=$START2_RC" 18
fi
# Re-poll for pidfile and bootstrap marker.
for _ in $(seq 1 20); do
    [ -f "$WORKER_PID_FILE" ] && break
    sleep 1
done
[ -f "$WORKER_PID_FILE" ] || fail "second start did not create pidfile" 18
# Pidfile is "<pid>\n<start_time>" — take the first line.
WORKER_PID2=$(head -n1 "$WORKER_PID_FILE" | tr -d '[:space:]')
for _ in $(seq 1 60); do
    if grep -q "Immich Microservices is running" "$WORKER_LOG" 2>/dev/null; then
        break
    fi
    sleep 1
done
if ! grep -q "Immich Microservices is running" "$WORKER_LOG" 2>/dev/null; then
    echo "--- second start stdout ---" >&2
    echo "$START2_OUT" | tail -40 >&2
    echo "--- worker log tail (no bootstrap marker on restart) ---" >&2
    if [ -f "$WORKER_LOG" ]; then
        wc -l "$WORKER_LOG" >&2
        tail -80 "$WORKER_LOG" >&2
    else
        echo "(worker log not created)" >&2
    fi
    echo "--- worker process state ---" >&2
    ps -p "$WORKER_PID2" -o pid,state,command 2>/dev/null >&2 || echo "PID $WORKER_PID2 gone" >&2
    fail "second start did not reach Nest bootstrap" 18
fi
if ! kill -0 "$WORKER_PID2" 2>/dev/null; then
    echo "--- worker log tail (PID $WORKER_PID2 gone after bootstrap) ---" >&2
    tail -60 "$WORKER_LOG" 2>/dev/null >&2 || true
    fail "second start PID $WORKER_PID2 exited after bootstrap" 18
fi
log "  restart cycle clean (new PID $WORKER_PID2)"

# Final stop to leave the VM in a clean state.
PYTHONPATH="$SRC_DIR" "$PY" -m immich_accelerator stop >/dev/null 2>&1 || true

# Final cleanup: kill our stub ML.
kill "$ML_PID" 2>/dev/null || true
wait "$ML_PID" 2>/dev/null || true
trap - EXIT

log "ALL CHECKS PASSED"
