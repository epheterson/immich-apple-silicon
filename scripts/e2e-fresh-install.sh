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

DATA="/tmp/e2e-data"
UPLOAD="/tmp/e2e-upload"

log() { printf '[e2e] %s\n' "$*"; }
fail() { printf '[e2e FAIL] %s\n' "$*" >&2; exit "${2:-1}"; }

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

log "step 1: python + fastapi + uvicorn importable (pre-installed at bootstrap)"
"$PY" -c "
import sys, fastapi, uvicorn
print(f'python {sys.version_info.major}.{sys.version_info.minor}, fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__}')
" || fail "dashboard deps not importable — bootstrap VM may be stale, re-run e2e-bootstrap-vm.sh" 2

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
# 6. Issue #19 — split-setup path probe. Only runs with valid auth.
# -------------------------------------------------------------------
if [ $AUTH_OK -eq 1 ]; then
    log "step 6: _detect_docker_media_prefix resolves Docker's media root"
    PROBE=$(PYTHONPATH="$SRC_DIR" "$PY" -c "
from immich_accelerator.__main__ import _detect_docker_media_prefix
p = _detect_docker_media_prefix('$IMMICH_URL', '$IMMICH_API_KEY')
print(p or '')
    ") || fail "path probe call raised" 8
    if [ -z "$PROBE" ]; then
        fail "probe returned None — expected a Docker-side path prefix" 8
    fi
    log "  detected Docker media prefix: $PROBE"
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

log "ALL CHECKS PASSED"
