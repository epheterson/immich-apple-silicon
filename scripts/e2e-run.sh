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

# Check immich_server is running
if ! "$HOME/.orbstack/bin/docker" ps --format '{{.Names}}' 2>/dev/null | grep -qx immich_server; then
    log "immich_server is not running in OrbStack. Aborting."
    exit 1
fi

# Resolve DB password from env Immich
DB_PASSWORD=$("$HOME/.orbstack/bin/docker" inspect immich_server \
    --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | awk -F= '/^DB_PASSWORD=/{print $2}')
if [ -z "$DB_PASSWORD" ]; then
    log "could not resolve DB_PASSWORD from immich_server env"
    exit 1
fi

# Immich API key from vault
if [ -f "$HOME/vault/secrets/services.yml" ] && command -v yq >/dev/null; then
    IMMICH_API_KEY=$(yq '.services.immich.api_key' "$HOME/vault/secrets/services.yml" 2>/dev/null)
fi
if [ -z "${IMMICH_API_KEY:-}" ] || [ "$IMMICH_API_KEY" = "null" ]; then
    log "IMMICH_API_KEY not found in vault and not set in env"
    exit 1
fi

# ------------------- Cleanup trap ----------------------
cleanup() {
    log "tearing down..."
    "$REPO_DIR/scripts/e2e-host-portforward.sh" stop 2>/dev/null || true
    tart stop --timeout 5 "$TEST_VM" 2>/dev/null || true
    tart delete "$TEST_VM" 2>/dev/null || true
    rm -f "$KEY_FILE" "$KEY_FILE.pub" 2>/dev/null || true
    log "cleanup done. (base VM and OCI image retained — run scripts/tart-cleanup.sh --all to free them)"
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
