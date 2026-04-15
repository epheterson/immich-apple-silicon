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

# Generate a throwaway ed25519 key BEFORE starting the VM so the
# single EXIT trap can always clean up both the key and the VM in
# one place. Earlier bug: appending to an existing trap via
# `trap -p` produced a malformed command string and crashed cleanup.
KEY_FILE="/tmp/iac-bootstrap-key-$$"
ssh-keygen -t ed25519 -N '' -f "$KEY_FILE" -q
KEY_PUB=$(cat "$KEY_FILE.pub")

log "Starting $BOOTSTRAP_VM (headless)..."
tart run --no-graphics "$BOOTSTRAP_VM" &
TART_PID=$!
# One trap, one place. Order matters: stop VM → kill tart proc →
# remove key files. Any step failing won't block the others.
trap '
    tart stop --timeout 5 "$BOOTSTRAP_VM" 2>/dev/null || true
    kill $TART_PID 2>/dev/null || true
    rm -f "$KEY_FILE" "$KEY_FILE.pub"
' EXIT

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

log "Installing Homebrew + python@3.11 + deps inside VM..."
# Write the bootstrap commands to a file on the VM first, then run
# the file. Heredoc-over-ssh is fragile: brew install reads/discards
# stdin during its parallel bottle downloads, which can eat or echo
# later heredoc lines before bash -s ever executes them. The bug was
# that `pip install fastapi uvicorn[standard]` showed up in the log
# as LITERAL text instead of being executed — never installed.
INNER_SCRIPT=$(mktemp -t iac-bootstrap-inner.XXXXXX)
cat > "$INNER_SCRIPT" <<'INNER'
#!/bin/bash
set -euo pipefail
# Skip brew's auto-update on every command — it flaky-fails on
# transient network issues and aborts the bootstrap when set -e
# is in effect. The cirruslabs base image is fresh enough that
# we don't need an update to find bottles.
export HOMEBREW_NO_AUTO_UPDATE=1
export HOMEBREW_NO_INSTALL_CLEANUP=1
export HOMEBREW_NO_ENV_HINTS=1
if ! command -v brew >/dev/null 2>&1; then
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
fi
eval "$(/opt/homebrew/bin/brew shellenv)"
# node@22 specifically — Immich 2.7.4 pins engines.node=24.14.1 and
# sharp@0.34.5 native addons won't load on the brew-default node 25.
# node@22 is keg-only so it must be linked explicitly via PATH order.
brew install --quiet python@3.11 git vips node@22 libpq
export PATH="/opt/homebrew/opt/node@22/bin:$PATH"
# Dashboard + stub ML service deps pre-installed so per-run E2E
# doesn't hit PyPI and doesn't need to build heavy ML wheels at
# test time. This set covers:
#   fastapi, uvicorn[standard] — dashboard (issue #17 coverage)
#   numpy, Pillow              — top-level imports of ml/src/main.py
#   python-multipart           — FastAPI multipart form handling
# Running `ml/src/main.py` in STUB_MODE is how we exercise the real
# /predict render path in the VM E2E without pulling ~2GB of mlx /
# onnxruntime / coreml. The stub mode is a first-class feature of
# the ml service, not a test hack.
#
# Retry the pip install up to 3 times — VM DNS is flaky and
# transient "nodename nor servname" errors are common. pip's own
# retries don't help because they fire BELOW DNS resolution.
for attempt in 1 2 3; do
    if /opt/homebrew/opt/python@3.11/bin/python3.11 -m pip install \
        --break-system-packages --quiet \
        fastapi 'uvicorn[standard]' numpy Pillow python-multipart; then
        break
    fi
    echo "pip install attempt $attempt failed, retrying in 5s..."
    sleep 5
    if [ "$attempt" = 3 ]; then
        echo "pip install failed after 3 attempts"
        exit 1
    fi
done
# Verify install before writing the marker — fail loud if anything
# didn't land where it should.
/opt/homebrew/bin/python3.11 -c "
import fastapi, uvicorn, numpy, PIL
print('bootstrap deps OK:',
      'fastapi', fastapi.__version__,
      'uvicorn', uvicorn.__version__,
      'numpy', numpy.__version__,
      'Pillow', PIL.__version__)
"

# Create the /build synthetic firmlink so Immich 2.7+ plugin paths
# resolve at runtime. This is EXACTLY what `immich-accelerator setup`
# does for real users — we do it during bootstrap so the base VM has
# /build active from first boot, letting the E2E exercise the real
# worker-start path (which verifies corePlugin/manifest.json at
# /build/corePlugin/manifest.json). Requires a reboot to take effect;
# the VM is stopped immediately after this and saved as the base,
# so when clones boot for E2E runs, /build is already live.
# Some macOS VM images only honor the legacy /etc/synthetic.conf
# (singular) even though /etc/synthetic.d/ is the supported location
# on macOS 11+. Write both to be safe. Both files must be owned by
# root and have tab-separated <name>\t<relative-path> entries.
sudo mkdir -p /etc/synthetic.d
ENTRY=$(printf 'build\tUsers/admin/.immich-accelerator/build-data')
echo "$ENTRY" | sudo tee /etc/synthetic.d/immich-accelerator >/dev/null
echo "$ENTRY" | sudo tee /etc/synthetic.conf >/dev/null
echo "wrote synthetic firmlink entry to both /etc/synthetic.d and /etc/synthetic.conf"
# Pre-create the build-data target dir so /build resolves cleanly.
# The E2E's step 3 populates this with corePlugin and geodata.
mkdir -p /Users/admin/.immich-accelerator/build-data

touch ~/.bootstrapped
echo "bootstrap inner script complete"
INNER
chmod +x "$INNER_SCRIPT"

# scp the file, then run it with a plain ssh — no stdin piping.
scp -i "$KEY_FILE" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o IdentitiesOnly=yes \
    "$INNER_SCRIPT" "$VM_USER@$VM_IP:/tmp/bootstrap-inner.sh"
ssh "${SSH_KEY_OPTS[@]}" "$VM_USER@$VM_IP" "bash /tmp/bootstrap-inner.sh"
rm -f "$INNER_SCRIPT"

# Reboot the VM so macOS reads /etc/synthetic.d/immich-accelerator
# and creates the /build firmlink. `apfs.util -t` at runtime fails
# with "failed to stitch firmlinks" on the root volume — a reboot
# is required. Then verify /build exists before saving the base.
log "Rebooting VM so /etc/synthetic.d/ takes effect..."
ssh "${SSH_KEY_OPTS[@]}" "$VM_USER@$VM_IP" "sudo shutdown -r now" 2>/dev/null || true
# Wait for the VM to go down (old PID disappears) then come back up.
sleep 15
for _ in $(seq 1 60); do
    if ssh "${SSH_KEY_OPTS[@]}" -o ConnectTimeout=3 \
        "$VM_USER@$VM_IP" "echo ok" 2>/dev/null | grep -q ok; then
        break
    fi
    sleep 2
done

log "Verifying /build firmlink after reboot..."
if ! ssh "${SSH_KEY_OPTS[@]}" "$VM_USER@$VM_IP" \
    "test -e /build && readlink /build" 2>&1; then
    log "/build is not active after reboot — synthetic.d did not take effect"
    ssh "${SSH_KEY_OPTS[@]}" "$VM_USER@$VM_IP" \
        "ls -la /; cat /etc/synthetic.d/immich-accelerator; od -c /etc/synthetic.d/immich-accelerator" 2>&1 || true
    exit 1
fi
log "/build synthetic firmlink verified post-reboot"

log "Stopping VM and saving snapshot..."
ssh "${SSH_KEY_OPTS[@]}" "$VM_USER@$VM_IP" "sudo shutdown -h now" 2>/dev/null || true
sleep 5
tart stop --timeout 5 "$BOOTSTRAP_VM" 2>/dev/null || true
trap - EXIT

log "Bootstrap complete. $BOOTSTRAP_VM is ready to be cloned by per-run E2E tests."
