#!/bin/bash
# scripts/e2e-run.sh
#
# Per-PR/per-release E2E runner. Clones the bootstrapped VM, runs the
# full fresh-install flow inside it against the host's Immich, tears
# down the clone. Cleanup rule: the VM is deleted on success
# AND on failure.
#
# Prereqs:
#   - scripts/e2e-bootstrap-vm.sh has been run once (creates immich-test-base)
#   - scripts/e2e-host-portforward.sh can be started (needs socat)
#   - the host's OrbStack is running the immich_server/postgres/redis stack
#
# Usage: scripts/e2e-run.sh

set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOOTSTRAP_VM="immich-test-base"
TS=$(date +%Y%m%d-%H%M%S)
TEST_VM="immich-test-run-$TS"
VM_USER="admin"
VM_PASSWORD="admin"
export SSHPASS="$VM_PASSWORD"

# Strategy: sshpass is fragile when ssh needs stdin (rsync, tar|ssh
# piping). Instead of fighting it, we use sshpass ONCE at the top
# of the run to install a throwaway ed25519 key into the VM, then
# use plain key-based ssh/rsync for everything after.
KEY_FILE="/tmp/iac-e2e-key-$$"
SSH_OPTS=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o ConnectTimeout=5
    -o IdentitiesOnly=yes
    -i "$KEY_FILE"
)
# For the first-contact sshpass call — must force password and
# disable all pubkey attempts so ssh doesn't try keys from agent
# before falling back to password.
SSH_OPTS_PW=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o ConnectTimeout=5
    -o PubkeyAuthentication=no
    -o PreferredAuthentications=password
    -o IdentitiesOnly=yes
)

log() { printf '[e2e-run %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# ------------------- Pre-flight ----------------------
if ! command -v tart >/dev/null; then
    log "tart missing. Run: brew install cirruslabs/cli/tart"
    exit 1
fi
if ! tart list | awk 'NR>1 {print $2}' | grep -qx "$BOOTSTRAP_VM"; then
    log "$BOOTSTRAP_VM missing. Run: scripts/e2e-bootstrap-vm.sh first."
    exit 1
fi
if ! command -v sshpass >/dev/null; then
    log "sshpass missing. Run: brew install hudochenkov/sshpass/sshpass"
    exit 1
fi
if ! command -v socat >/dev/null; then
    log "socat missing. Run: brew install socat"
    exit 1
fi

# ------------------- Isolated Immich stack (precondition) ---------
# The VM harness used to forward traffic straight at the developer's
# prod Immich. A test worker booting with IMMICH_MEDIA_LOCATION=
# /tmp/e2e-upload then wrote that path into prod's system_metadata
# and ran a blanket path rewrite across every asset_file row. The
# harness now refuses to target anything but the dedicated e2e
# stack defined in scripts/e2e-stack.yml — postgres + redis + api-
# only Immich server on port-shifted loopback addresses with
# throwaway state.
#
# The stack is a PRECONDITION, not something this script brings up
# itself. That's intentional: the stack takes ~4 minutes to become
# healthy (Immich's schema migrations run on first boot), and
# launching a 4-minute fixture inside a shell subprocess turned out
# to be unreliable. Instead, run it once manually, let multiple
# E2E iterations reuse it, then tear it down yourself:
#
#   scripts/e2e-stack.sh up       # one-time, ~4 min
#   scripts/e2e-run.sh             # fast, ~90s, can repeat
#   scripts/e2e-stack.sh down     # when you're done
#
# If the stack isn't up, refuse to run. We will NOT fall back to
# prod Immich — that's the whole point of this refactor.
if ! curl -sf http://127.0.0.1:22283/api/server/ping >/dev/null 2>&1; then
    log "Isolated e2e stack not reachable at 127.0.0.1:22283."
    log "Bring it up first with: scripts/e2e-stack.sh up"
    log "(Refusing to fall back to prod Immich — that caused the "
    log " /tmp/e2e-upload DB pollution incident on 2026-04-15.)"
    exit 1
fi
if [ ! -s /tmp/immich-e2e-stack/api-key ]; then
    log "Isolated stack is running but has no API key cached."
    log "Re-run: scripts/e2e-stack.sh up"
    exit 1
fi
DB_PASSWORD="e2epass"
IMMICH_API_KEY=$("$REPO_DIR/scripts/e2e-stack.sh" api-key)
log "using isolated stack: API key ${IMMICH_API_KEY:0:8}... (len ${#IMMICH_API_KEY})"

# Tell the port forwarder where to send VM traffic. The VM-side
# ports (12283/15432/16379) stay the same because that's what the
# in-VM E2E script hardcodes; only the host-side destination shifts
# from prod defaults to the isolated stack's port-shifted layout.
export E2E_DST_IMMICH_PORT=22283
export E2E_DST_DB_PORT=25432
export E2E_DST_REDIS_PORT=26379

# ------------------- Cleanup trap ----------------------
# Installed after the stack precondition check so we don't paper
# over a missing stack with a trap. The trap only tears down the
# VM + port forwarders — the isolated stack persists across runs
# and is managed manually via scripts/e2e-stack.sh.
cleanup() {
    log "tearing down..."
    "$REPO_DIR/scripts/e2e-host-portforward.sh" stop 2>/dev/null || true
    tart stop --timeout 5 "$TEST_VM" 2>/dev/null || true
    tart delete "$TEST_VM" 2>/dev/null || true
    rm -f "$KEY_FILE" "$KEY_FILE.pub" 2>/dev/null || true
    log "cleanup done. Isolated stack left running — tear it down"
    log "when finished with: scripts/e2e-stack.sh down"
}
trap cleanup EXIT

# Generate a throwaway ed25519 keypair for this run. Saves us from
# fighting sshpass's pty weirdness for every rsync / ssh with stdin.
ssh-keygen -t ed25519 -N '' -f "$KEY_FILE" -q
KEY_PUB=$(cat "$KEY_FILE.pub")

# ------------------- Clone + start VM ----------------------
# Use default (Shared) networking — softnet requires passwordless
# sudo and we don't need its layer-2 features.
log "cloning $BOOTSTRAP_VM -> $TEST_VM"
tart clone "$BOOTSTRAP_VM" "$TEST_VM"

log "starting $TEST_VM"
tart run --no-graphics "$TEST_VM" &
TART_PID=$!

# Wait for IP + SSH
log "waiting for VM IP..."
VM_IP=""
for _ in $(seq 1 60); do
    VM_IP=$(tart ip "$TEST_VM" 2>/dev/null || true)
    if [ -n "$VM_IP" ]; then break; fi
    sleep 2
done
[ -z "$VM_IP" ] && { log "VM never acquired IP"; exit 3; }
log "VM IP: $VM_IP"

# Derive the host bridge IP from the VM's subnet (always X.X.X.1 in
# tart's Shared-mode NAT) so socat can bind to the bridge interface
# that now exists.
HOST_BRIDGE_IP="$(echo "$VM_IP" | awk -F. '{print $1"."$2"."$3".1"}')"
log "host bridge IP from VM's perspective: $HOST_BRIDGE_IP"

# ------------------- Host port forwarders ----------------------
# Must come AFTER the VM starts so the bridge interface is up.
log "starting host port forwarders ($HOST_BRIDGE_IP -> 127.0.0.1)"
HOST_BIND_IP="$HOST_BRIDGE_IP" "$REPO_DIR/scripts/e2e-host-portforward.sh" start

log "waiting for VM SSH (password auth, one-time key install)..."
for _ in $(seq 1 30); do
    # sshpass with password auth for the FIRST contact only — we
    # use this single call to install our throwaway pubkey into
    # ~/.ssh/authorized_keys so every subsequent ssh/rsync uses
    # clean key auth. This sidesteps sshpass's pty fragility
    # with rsync and tar pipes.
    if sshpass -e ssh "${SSH_OPTS_PW[@]}" "$VM_USER@$VM_IP" \
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
         echo '$KEY_PUB' >> ~/.ssh/authorized_keys && \
         chmod 600 ~/.ssh/authorized_keys && \
         echo keyed" 2>/dev/null | grep -q keyed; then
        break
    fi
    sleep 2
done

# Sanity: verify key auth works now.
if ! ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" "echo ok" 2>/dev/null | grep -q ok; then
    log "VM SSH key auth failed after install — aborting."
    exit 4
fi
log "VM SSH key auth OK"

# ------------------- Ship the source under test via rsync ----------
# From here on, every call uses plain ssh with our installed key.
# No sshpass, no pty tricks, no stdin conflicts.
log "rsyncing source + e2e script into VM"
ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" "mkdir -p /tmp/iac-src"
rsync -az --delete \
    --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    -e "ssh ${SSH_OPTS[*]}" \
    "$REPO_DIR/immich_accelerator" \
    "$REPO_DIR/ml" \
    "$REPO_DIR/tests" \
    "$REPO_DIR/VERSION" \
    "$REPO_DIR/scripts" \
    "$VM_USER@$VM_IP:/tmp/iac-src/"

# ------------------- Run test ----------------------
log "running E2E inside VM (host reachable at $HOST_BRIDGE_IP)..."
set +e
ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
    "set -e; \
     SRC_DIR=/tmp/iac-src \
     IMMICH_URL=http://$HOST_BRIDGE_IP:12283 \
     IMMICH_API_KEY='$IMMICH_API_KEY' \
     DB_HOST=$HOST_BRIDGE_IP DB_PORT=15432 DB_PASSWORD='$DB_PASSWORD' \
     REDIS_HOST=$HOST_BRIDGE_IP REDIS_PORT=16379 \
     bash /tmp/iac-src/scripts/e2e-fresh-install.sh"
RC=$?
set -e

if [ $RC -eq 0 ]; then
    log "E2E PASSED"
else
    log "E2E FAILED (exit $RC)"
fi

exit $RC
