#!/usr/bin/env bash
# Build a relocatable bundle of the native ML service: the release binary with
# mlx.metallib and libonnxruntime colocated (@loader_path), ad-hoc signed so it
# runs after relocation. Run on Apple Silicon with the Swift toolchain + brew
# onnxruntime installed.
set -euo pipefail
cd "$(dirname "$0")/.."   # native-ml/
export PATH=/opt/homebrew/bin:$PATH

OUT="${1:-bundle}"
ORT="$(brew --prefix onnxruntime)"
METALLIB="${MLX_METALLIB:-$(find /opt/homebrew -name mlx.metallib 2>/dev/null | head -1)}"
[ -n "$METALLIB" ] || { echo "mlx.metallib not found (set MLX_METALLIB)"; exit 1; }

echo "building release..."
swift build -c release >/dev/null

rm -rf "$OUT"; mkdir -p "$OUT"
cp .build/release/immich-ml-native "$OUT/"
cp "$METALLIB" "$OUT/mlx.metallib"

# Resolve the onnxruntime dylib version-agnostically: the real versioned file
# (e.g. libonnxruntime.1.27.1.dylib) and the exact path the binary links to
# (whatever brew's onnxruntime version put there). Hardcoding a version breaks
# on any runner/machine with a different onnxruntime.
ORT_REAL="$(find "$ORT/lib" -name 'libonnxruntime.*.*.*.dylib' | head -1)"
ORT_LINK="$(otool -L "$OUT/immich-ml-native" | grep -oE '[^[:space:]]*libonnxruntime[^[:space:]]*\.dylib' | head -1)"
[ -n "$ORT_REAL" ] && [ -n "$ORT_LINK" ] || { echo "could not resolve onnxruntime dylib (real=$ORT_REAL link=$ORT_LINK)"; exit 1; }
cp "$ORT_REAL" "$OUT/libonnxruntime.1.dylib"

# make onnxruntime load from beside the binary, not the brew prefix
install_name_tool -id @loader_path/libonnxruntime.1.dylib "$OUT/libonnxruntime.1.dylib"
install_name_tool -change "$ORT_LINK" \
    @loader_path/libonnxruntime.1.dylib "$OUT/immich-ml-native"

# re-sign ad-hoc (install_name_tool invalidates the signature)
codesign -f -s - "$OUT/libonnxruntime.1.dylib" >/dev/null 2>&1 || true
codesign -f -s - "$OUT/immich-ml-native" >/dev/null 2>&1 || true

echo "bundle -> $OUT"
ls -la "$OUT"
echo "linkage:"
otool -L "$OUT/immich-ml-native" | grep -iE "onnxruntime|@loader" || true
