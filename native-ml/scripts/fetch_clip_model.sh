#!/usr/bin/env bash
# Fetch the CLIP ViT-B-32 model the native engine needs (model.safetensors +
# merges.txt + vocab.json) into ~/.cache/immich-ml-native/clip. Shipped as a
# release asset rather than pulled from HuggingFace: openai/clip-vit-base-patch32
# ships pytorch_model.bin, not safetensors, so we host the exact validated
# weights to guarantee embedding parity with the index. ~570MB, one time.
set -euo pipefail
DEST="${1:-$HOME/.cache/immich-ml-native/clip}"
URL="${2:-${CLIP_MODEL_URL:?set CLIP_MODEL_URL to the clip-vit-base-patch32 model tarball release asset}}"

if [ -s "$DEST/model.safetensors" ] && [ -s "$DEST/merges.txt" ] && [ -s "$DEST/vocab.json" ]; then
    echo "CLIP model already present at $DEST"; exit 0
fi

mkdir -p "$DEST"
echo "fetching CLIP model from $URL ..."
tmp="$(mktemp)"
curl -fL --retry 3 -o "$tmp" "$URL"
tar -xzf "$tmp" -C "$DEST" --strip-components=1
rm -f "$tmp"
echo "CLIP model ready at $DEST: $(ls "$DEST" | tr '\n' ' ')"
