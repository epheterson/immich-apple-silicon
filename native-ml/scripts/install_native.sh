#!/usr/bin/env bash
# Install the native Swift ML engine for the accelerator: build the relocatable
# bundle into ~/.immich-accelerator/native-ml and stage the CLIP safetensors
# model into ~/.cache/immich-ml-native/clip. Does NOT flip the engine — enable
# it by setting "ml_engine": "native" in the accelerator config and restarting.
#
# Usage: install_native.sh [path-to-clip-safetensors-dir]
# The CLIP dir must contain model.safetensors (vision+text) + merges.txt + vocab.json
# (e.g. openai/clip-vit-base-patch32). ArcFace comes from ~/.insightface (buffalo_l).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/homebrew/bin:$PATH

DEST="$HOME/.immich-accelerator/native-ml"
CLIP_DEST="$HOME/.cache/immich-ml-native/clip"
CLIP_SRC="${1:-}"

echo "==> building relocatable bundle -> $DEST"
bash scripts/build_bundle.sh "$DEST" >/dev/null
echo "    bundle: $(ls "$DEST" | tr '\n' ' ')"

mkdir -p "$CLIP_DEST"
if [ -n "$CLIP_SRC" ] && [ -d "$CLIP_SRC" ]; then
    echo "==> staging CLIP model from $CLIP_SRC"
    cp "$CLIP_SRC"/*.safetensors "$CLIP_DEST/"
    cp "$CLIP_SRC"/merges.txt "$CLIP_SRC"/vocab.json "$CLIP_DEST/" 2>/dev/null || true
fi
if ls "$CLIP_DEST"/*.safetensors >/dev/null 2>&1; then
    echo "    CLIP model: $(ls "$CLIP_DEST" | tr '\n' ' ')"
else
    echo "    WARNING: no CLIP safetensors at $CLIP_DEST — pass the model dir as arg 1"
fi

echo
echo "Native engine staged. Enable it with:  \"ml_engine\": \"native\"  in the config, then restart."
