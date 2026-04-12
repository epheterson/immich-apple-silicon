#!/bin/bash
# scripts/e2e-host-portforward.sh
#
# Ephemeral socat forwarders that expose the host's 127.0.0.1-bound
# Immich services on 192.168.64.1 so the tart VM can reach them.
#
# Starts three forwarders, writes their PIDs to a pidfile, and
# tears them down on signal or on `--stop`. Does NOT modify the
# underlying docker-compose or port bindings.

set -euo pipefail

PIDFILE="/tmp/immich-e2e-portforward.pid"
# HOST_BIND_IP is the bridge interface the VM will reach the host on.
# In tart's Shared NAT mode this is the X.X.X.1 of the VM's subnet.
# Caller passes it in so we don't have to guess — VM IP is only
# known after `tart run` starts.
HOST_BIND="${HOST_BIND_IP:-192.168.64.1}"

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
    for pair in "12283:2283" "15432:5432" "16379:6379"; do
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
