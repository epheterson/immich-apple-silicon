#!/bin/bash
# scripts/e2e-host-portforward.sh
#
# Ephemeral socat forwarders that expose host-loopback TCP services
# on the host bridge IP so the tart VM can reach them.
#
# Source ports (VM-side) are fixed — they're what e2e-fresh-install
# hard-codes into its test config. Destination ports (host-side)
# are parameterized via env so callers can point the harness at
# either the developer's prod Immich (the old, dangerous default)
# OR the isolated e2e stack from scripts/e2e-stack.yml (now the
# only sanctioned mode).
#
# Env overrides:
#   E2E_DST_IMMICH_PORT  host port for Immich API (default 2283)
#   E2E_DST_DB_PORT      host port for Postgres  (default 5432)
#   E2E_DST_REDIS_PORT   host port for Redis     (default 6379)
#
# The defaults still point at the prod ports for backwards-compat
# invocations, but e2e-run.sh sets all three to the isolated stack
# ports (22283/25432/26379) before calling into this script. If
# you're running the harness by hand, set them explicitly.

set -euo pipefail

PIDFILE="/tmp/immich-e2e-portforward.pid"
# HOST_BIND_IP is the bridge interface the VM will reach the host on.
# In tart's Shared NAT mode this is the X.X.X.1 of the VM's subnet.
# Caller passes it in so we don't have to guess — VM IP is only
# known after `tart run` starts.
HOST_BIND="${HOST_BIND_IP:-192.168.64.1}"

# Destination ports on the host loopback. Defaults match the old
# prod layout; e2e-run.sh overrides these for isolated stack mode.
DST_IMMICH="${E2E_DST_IMMICH_PORT:-2283}"
DST_DB="${E2E_DST_DB_PORT:-5432}"
DST_REDIS="${E2E_DST_REDIS_PORT:-6379}"

export PATH="/opt/homebrew/bin:$PATH"

if ! command -v socat >/dev/null; then
    echo "socat not installed. Run: brew install socat" >&2
    exit 1
fi

start_forwarders() {
    if [ -f "$PIDFILE" ]; then
        # Orphan pidfile from a previous crashed run. Stop any live
        # PIDs (may no longer exist) and blow the file away — this
        # is a developer tool, not something that needs to be
        # paranoid about colliding with a legitimate running copy.
        echo "Cleaning up stale pidfile $PIDFILE..."
        stop_forwarders
    fi
    : > "$PIDFILE"
    # src:dst pairs. VM-side source ports are fixed; host-side dst
    # ports come from env so the caller decides whether we talk to
    # isolated e2e stack or (deprecated) prod Immich.
    for pair in "12283:$DST_IMMICH" "15432:$DST_DB" "16379:$DST_REDIS"; do
        src="${pair%:*}"; dst="${pair#*:}"
        socat TCP-LISTEN:"$src",bind="$HOST_BIND",fork,reuseaddr TCP:127.0.0.1:"$dst" &
        echo $! >> "$PIDFILE"
        echo "forwarder: $HOST_BIND:$src -> 127.0.0.1:$dst (pid $!)"
    done
}

stop_forwarders() {
    if [ ! -f "$PIDFILE" ]; then
        echo "No forwarders running."
        return 0
    fi
    while read -r pid; do
        kill "$pid" 2>/dev/null || true
    done < "$PIDFILE"
    rm -f "$PIDFILE"
    echo "forwarders stopped."
}

case "${1:-start}" in
    start) start_forwarders ;;
    stop)  stop_forwarders ;;
    *) echo "usage: $0 [start|stop]" >&2; exit 2 ;;
esac
