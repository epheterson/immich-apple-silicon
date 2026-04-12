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

VENV="/tmp/e2e-venv"
DATA="/tmp/e2e-data"
UPLOAD="/tmp/e2e-upload"

log() { printf '[e2e] %s\n' "$*"; }
fail() { printf '[e2e FAIL] %s\n' "$*" >&2; exit "${2:-1}"; }

rm -rf "$DATA" "$UPLOAD"
mkdir -p "$DATA" "$UPLOAD"

# -------------------------------------------------------------------
# 1. Build a pristine venv with only the deps the formula pins.
#    This mirrors the ML venv the formula creates at post_install.
# -------------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    log "step 1: create venv with fastapi + uvicorn[standard]"
    /opt/homebrew/bin/python3.11 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet fastapi 'uvicorn[standard]'
else
    log "step 1: reusing existing venv at $VENV"
fi

PY="$VENV/bin/python"

# Smoke check: Python version + fastapi+uvicorn are importable.
"$PY" -c "
import sys, fastapi, uvicorn
print(f'python {sys.version_info.major}.{sys.version_info.minor}, fastapi {fastapi.__version__}, uvicorn {uvicorn.__version__}')
" || fail "venv smoke check failed" 2

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

# -------------------------------------------------------------------
# Done. Graceful shutdown.
# -------------------------------------------------------------------
kill "$DASH_PID" 2>/dev/null || true
trap - EXIT

log "ALL CHECKS PASSED"
