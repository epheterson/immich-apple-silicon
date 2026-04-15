#!/bin/bash
# scripts/e2e-stack.sh
#
# Lifecycle + API-key bootstrap for the isolated Immich E2E stack
# defined in scripts/e2e-stack.yml. Replaces the old path of pointing
# the VM harness at the developer's prod Immich.
#
# Usage:
#   scripts/e2e-stack.sh up      # docker-compose up -d + wait-healthy
#                                # + admin-sign-up + API key
#   scripts/e2e-stack.sh down    # docker-compose down -v + rm volumes
#   scripts/e2e-stack.sh api-key # print the cached API key from up
#   scripts/e2e-stack.sh info    # print URLs/ports the VM should use
#
# After `up`, the stack reachable on the host loopback at:
#   Immich API : http://127.0.0.1:22283
#   Postgres   : 127.0.0.1:25432  user=postgres pw=e2epass db=immich
#   Redis      : 127.0.0.1:26379
#
# The API key is written to /tmp/immich-e2e-stack/api-key and the
# admin user is a throwaway (e2e@example.com / e2epass). The whole
# stack and state dir disappear on `down`.

set -euo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/opt/libpq/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/e2e-stack.yml"
STATE_DIR="/tmp/immich-e2e-stack"
API_KEY_FILE="$STATE_DIR/api-key"

IMMICH_URL="http://127.0.0.1:22283"
ADMIN_EMAIL="e2e@example.com"
ADMIN_PASSWORD="e2epass"
ADMIN_NAME="E2E Admin"

# Prefer OrbStack's docker binary (used throughout the harness) but
# fall back to anything on PATH — most dev machines have one or the
# other but not both.
DOCKER_BIN=""
for candidate in \
    "$HOME/.orbstack/bin/docker" \
    "/opt/homebrew/bin/docker" \
    "/usr/local/bin/docker"
do
    if [ -x "$candidate" ]; then
        DOCKER_BIN="$candidate"
        break
    fi
done
if [ -z "$DOCKER_BIN" ]; then
    echo "docker not found — install OrbStack or Docker Desktop" >&2
    exit 1
fi

log() { printf '[e2e-stack] %s\n' "$*"; }

# -----------------------------------------------------------------
# safety: refuse to run if prod Immich containers exist on the same
# host, which would be a strong signal that someone's about to
# reintroduce the exact pollution we just fixed. The check is
# non-fatal — we only ABORT if the test ports are in use BY
# something other than our own containers.
# -----------------------------------------------------------------
check_port_conflicts() {
    for port in 22283 25432 26379; do
        if lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then
            # Is it one of our own containers from a prior run?
            if "$DOCKER_BIN" ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
                | grep -q "immich_.*_e2e.*:$port->"; then
                continue
            fi
            echo "port $port is already in use by something other than the e2e stack" >&2
            echo "if that's prod Immich, you DO NOT want to run the E2E without isolation" >&2
            echo "kill whatever is on $port and retry" >&2
            exit 2
        fi
    done
}

# -----------------------------------------------------------------
# bring up the compose stack and wait for all three services to be
# healthy. The healthcheck on immich_server_e2e ultimately hits
# /api/server/ping, which only answers 200 after DB migrations are
# done, so once this returns we're safe to call admin-sign-up.
# -----------------------------------------------------------------
cmd_up() {
    check_port_conflicts
    mkdir -p "$STATE_DIR/pg" "$STATE_DIR/upload"
    log "compose up"
    "$DOCKER_BIN" compose -f "$COMPOSE_FILE" up -d

    log "waiting for services to become healthy..."
    for i in $(seq 1 120); do
        # docker compose ps --format json gives health state in each
        # service's `Health` field. All-healthy = ready.
        states=$("$DOCKER_BIN" compose -f "$COMPOSE_FILE" ps --format json 2>/dev/null \
            | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        svc = json.loads(line)
        print(svc.get('Health', svc.get('State', '?')))
    except json.JSONDecodeError:
        pass
" || true)
        count_healthy=$(echo "$states" | grep -c '^healthy$' || true)
        if [ "$count_healthy" = "3" ]; then
            log "all 3 services healthy"
            break
        fi
        if [ $((i % 10)) = 0 ]; then
            log "  still waiting ($i/120)... states: $(echo "$states" | tr '\n' ' ')"
        fi
        sleep 2
    done
    # Final gate: hit the Immich API ping directly.
    for i in $(seq 1 30); do
        if curl -sf "$IMMICH_URL/api/server/ping" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if ! curl -sf "$IMMICH_URL/api/server/ping" >/dev/null 2>&1; then
        log "API did not answer /api/server/ping — dumping logs and bailing"
        "$DOCKER_BIN" compose -f "$COMPOSE_FILE" logs --tail=40
        exit 3
    fi
    log "API reachable"

    bootstrap_api_key
    log "stack ready. API=$IMMICH_URL api-key=$(cat "$API_KEY_FILE")"
}

# -----------------------------------------------------------------
# Create the first admin user via /api/auth/admin-sign-up, then log
# in to receive a session cookie, then create a long-lived API key.
# All three endpoints are Immich's own first-boot bootstrap path —
# we're not doing anything special, just scripting what the UI's
# onboarding wizard does.
# -----------------------------------------------------------------
bootstrap_api_key() {
    if [ -s "$API_KEY_FILE" ]; then
        log "API key already bootstrapped"
        return 0
    fi
    log "bootstrapping admin user + API key..."

    # admin-sign-up is a no-op 400 after the first run (user exists),
    # which is fine — we'll fall through to login either way.
    curl -sf -X POST "$IMMICH_URL/api/auth/admin-sign-up" \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\",\"name\":\"$ADMIN_NAME\"}" \
        >/dev/null || true

    TOKEN=$(curl -sf -X POST "$IMMICH_URL/api/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['accessToken'])")
    if [ -z "${TOKEN:-}" ]; then
        log "login failed after admin-sign-up — cannot bootstrap API key"
        exit 4
    fi

    KEY=$(curl -sf -X POST "$IMMICH_URL/api/api-keys" \
        -H 'Content-Type: application/json' \
        -H "Authorization: Bearer $TOKEN" \
        -d '{"name":"e2e-harness","permissions":["all"]}' \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['secret'])")
    if [ -z "${KEY:-}" ]; then
        log "api-keys POST failed"
        exit 5
    fi
    echo -n "$KEY" > "$API_KEY_FILE"
    chmod 600 "$API_KEY_FILE"
    log "API key written to $API_KEY_FILE"
}

cmd_down() {
    log "compose down -v"
    "$DOCKER_BIN" compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    # The compose volume bindings are bind mounts so `down -v` doesn't
    # remove them — clean them ourselves. rm -rf /tmp/immich-e2e-stack
    # is safe by design: the whole point of $STATE_DIR is that it's
    # throwaway.
    if [ -d "$STATE_DIR" ]; then
        rm -rf "$STATE_DIR"
        log "removed $STATE_DIR"
    fi
}

cmd_api_key() {
    if [ ! -s "$API_KEY_FILE" ]; then
        echo "no API key on disk — run \`$0 up\` first" >&2
        exit 1
    fi
    cat "$API_KEY_FILE"
}

cmd_info() {
    cat <<INFO
IMMICH_URL       = $IMMICH_URL
DB_HOST          = 127.0.0.1
DB_PORT          = 25432
DB_USER          = postgres
DB_PASSWORD      = e2epass
DB_NAME          = immich
REDIS_HOST       = 127.0.0.1
REDIS_PORT       = 26379
API_KEY_FILE     = $API_KEY_FILE
ADMIN_EMAIL      = $ADMIN_EMAIL
ADMIN_PASSWORD   = $ADMIN_PASSWORD
INFO
}

case "${1:-}" in
    up)      cmd_up ;;
    down)    cmd_down ;;
    api-key) cmd_api_key ;;
    info)    cmd_info ;;
    *) echo "usage: $0 {up|down|api-key|info}" >&2; exit 2 ;;
esac
