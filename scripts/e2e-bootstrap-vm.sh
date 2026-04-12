#!/bin/bash
# scripts/e2e-bootstrap-vm.sh
#
# One-time setup: clone the macOS Sonoma base image into a reusable VM
# with Homebrew + python@3.11 + git installed, then snapshot it as the
# baseline every per-PR test clones from. Saves ~10 minutes per run.
#
# Run on the Apple Silicon test host. Idempotent — skips work already done.
#
# Peak disk cost: ~60GB (base image + bootstrap VM clone).
# Cleanup: scripts/tart-cleanup.sh --all

set -euo pipefail

BASE_IMAGE="ghcr.io/cirruslabs/macos-sonoma-base:latest"
BOOTSTRAP_VM="immich-test-base"
VM_USER="admin"
VM_PASSWORD="admin"

export PATH="/opt/homebrew/bin:$PATH"
# sshpass -e reads the password from SSHPASS, freeing stdin for
# piped commands and avoiding the tty-detection fragility of -p.
export SSHPASS="$VM_PASSWORD"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

if ! command -v tart >/dev/null; then
    log "tart not installed. Run: brew install cirruslabs/cli/tart"
    exit 1
fi

# 1. Pull base image if missing. `tart list` shows OCI images by
#    their full URL in the second column — grep against that.
if ! tart list 2>/dev/null | grep -q "$BASE_IMAGE"; then
    log "Pulling base image $BASE_IMAGE (~30GB, one-time)..."
    tart pull "$BASE_IMAGE"
else
    log "Base image already pulled."
fi

# 2. Clone to bootstrap VM if missing. Clones MUST use the full OCI
#    URL as the source — tart does not expose short names for cached
#    OCI images.
if ! tart list --format json 2>/dev/null | grep -q "\"Name\":\"$BOOTSTRAP_VM\""; then
    log "Cloning $BASE_IMAGE into $BOOTSTRAP_VM..."
    tart clone "$BASE_IMAGE" "$BOOTSTRAP_VM"
fi

# 3. Refuse to double-start — if something else is already running
#    the bootstrap VM, bail instead of interfering.
running=$(tart list --format json 2>/dev/null | python3 -c "
import sys, json
for vm in json.load(sys.stdin):
    if vm.get('Name') == '$BOOTSTRAP_VM' and vm.get('Running'):
        print('yes')
" 2>/dev/null || true)
if [ "$running" = "yes" ]; then
    log "$BOOTSTRAP_VM is already running. Bailing to avoid collision."
    exit 2
fi

log "Starting $BOOTSTRAP_VM (headless)..."
tart run --no-graphics "$BOOTSTRAP_VM" &
TART_PID=$!
trap 'tart stop --timeout 5 "$BOOTSTRAP_VM" 2>/dev/null || true; kill $TART_PID 2>/dev/null || true' EXIT

# Wait for VM IP
log "Waiting for VM to boot and acquire IP..."
VM_IP=""
for _ in $(seq 1 60); do
    VM_IP=$(tart ip "$BOOTSTRAP_VM" 2>/dev/null || true)
    if [ -n "$VM_IP" ]; then break; fi
    sleep 2
done
if [ -z "$VM_IP" ]; then
    log "VM did not acquire IP within 2 minutes. Aborting."
    exit 3
fi
log "VM IP: $VM_IP"

# Generate a throwaway ed25519 key for this bootstrap run. Same
# sshpass pty/stdin fragility as e2e-run.sh — we use the key for
# every call after the initial password-auth install, which frees
# us to use heredocs and any stdin redirection we want.
KEY_FILE="/tmp/iac-bootstrap-key-$$"
ssh-keygen -t ed25519 -N '' -f "$KEY_FILE" -q
KEY_PUB=$(cat "$KEY_FILE.pub")
KEY_TRAP_OLD=$(trap -p EXIT)
trap "${KEY_TRAP_OLD#trap -- }; rm -f $KEY_FILE $KEY_FILE.pub" EXIT

SSH_PW_OPTS=(
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
    -o ConnectTimeout=5
    -o PubkeyAuthentication=no
    -o PreferredAuthentications=password
    -o IdentitiesOnly=yes
)
SSH_KEY_OPTS=(
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
    -o ConnectTimeout=5
    -o IdentitiesOnly=yes
    -i "$KEY_FILE"
)

# Wait for SSH + install pubkey in one shot
log "Waiting for SSH (password auth) + installing throwaway key..."
for _ in $(seq 1 30); do
    if sshpass -e ssh "${SSH_PW_OPTS[@]}" "$VM_USER@$VM_IP" \
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
         echo '$KEY_PUB' >> ~/.ssh/authorized_keys && \
         chmod 600 ~/.ssh/authorized_keys && \
         echo keyed" 2>/dev/null | grep -q keyed; then
        break
    fi
    sleep 2
done
if ! ssh "${SSH_KEY_OPTS[@]}" "$VM_USER@$VM_IP" "echo ok" 2>/dev/null | grep -q ok; then
    log "Key auth failed after install — aborting."
    exit 4
fi
log "Key auth OK"

# Check for bootstrap marker — if present, we're already done
if ssh "${SSH_KEY_OPTS[@]}" "$VM_USER@$VM_IP" "test -f /Users/$VM_USER/.bootstrapped" 2>/dev/null; then
    log "VM is already bootstrapped. Stopping and exiting."
    tart stop "$BOOTSTRAP_VM"
    trap - EXIT
    rm -f "$KEY_FILE" "$KEY_FILE.pub"
    exit 0
fi

log "Installing Homebrew + python@3.11 + git + deps inside VM..."
ssh "${SSH_KEY_OPTS[@]}" "$VM_USER@$VM_IP" 'bash -s' <<'INNER'
set -euo pipefail
if ! command -v brew >/dev/null; then
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
fi
eval "$(/opt/homebrew/bin/brew shellenv)"
brew install --quiet python@3.11 git vips node libpq
# Pre-install dashboard deps into system python@3.11 so per-run
# E2E tests don't hit PyPI (which has flaky DNS inside the VM).
# Same package composition as ml/requirements.txt, so this is a
# faithful proxy for what the Homebrew formula's ml venv ships.
/opt/homebrew/opt/python@3.11/bin/python3.11 -m pip install \
    --break-system-packages --quiet \
    fastapi 'uvicorn[standard]'
touch ~/.bootstrapped
INNER

log "Stopping VM and saving snapshot..."
ssh "${SSH_KEY_OPTS[@]}" "$VM_USER@$VM_IP" "sudo shutdown -h now" 2>/dev/null || true
sleep 5
tart stop --timeout 5 "$BOOTSTRAP_VM" 2>/dev/null || true
trap - EXIT

log "Bootstrap complete. $BOOTSTRAP_VM is ready to be cloned by per-run E2E tests."
