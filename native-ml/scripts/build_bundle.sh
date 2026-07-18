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

# Recursively vendor EVERY remaining Homebrew (non-system) dylib dependency, so
# the bundle is self-contained. brew's onnxruntime dynamically links onnx,
# onnx_proto, protobuf, re2, and a pile of abseil libs (which pull in more).
# Copying only libonnxruntime meant the binary dyld-crashed at startup on any
# machine that didn't happen to have all those formulae installed (i.e. every
# real user), so the accelerator silently fell back to Python (#111). Copy each
# dep into the bundle and point its id + every reference at @loader_path.
vendor_deps() {
    local todo=("$OUT/immich-ml-native" "$OUT/libonnxruntime.1.dylib")
    local seen="" cur deps dep base dest
    while [ ${#todo[@]} -gt 0 ]; do
        cur="${todo[0]}"; todo=("${todo[@]:1}")
        case " $seen " in *" $cur "*) continue ;; esac
        seen="$seen $cur"
        # Exclude the file's own LC_ID_DYLIB (otool -L lists it first for a
        # dylib) so a lib whose id is still an absolute path isn't mistaken for
        # a dependency of itself.
        selfid="$(otool -D "$cur" | sed -n '2p')"
        deps="$(otool -L "$cur" | awk 'NR>1 && $1 ~ /^\/opt\/homebrew.*\.dylib$/ {print $1}' | grep -vxF "$selfid" || true)"
        while IFS= read -r dep; do
            [ -n "$dep" ] || continue
            base="$(basename "$dep")"
            dest="$OUT/$base"
            if [ ! -f "$dest" ]; then
                cp "$dep" "$dest"
                chmod u+w "$dest"
                install_name_tool -id "@loader_path/$base" "$dest"
                todo+=("$dest")
            fi
            install_name_tool -change "$dep" "@loader_path/$base" "$cur"
        done <<< "$deps"
    done
}
vendor_deps

# The abseil/protobuf/onnx libs reference each other by @rpath, which only
# resolves because the binary carries a machine-specific rpath (built with
# -rpath /opt/homebrew/opt/onnxruntime/lib, see Package.swift). Repoint every
# @rpath dep we actually vendored to @loader_path so nothing relies on that
# rpath. @rpath deps to OS libraries (e.g. libswift* in /usr/lib/swift) have no
# vendored copy and are left alone.
for f in "$OUT/immich-ml-native" "$OUT"/*.dylib; do
    otool -L "$f" | awk 'NR>1 && $1 ~ /^@rpath\/.*\.dylib$/ {print $1}' | while IFS= read -r rdep; do
        rbase="$(basename "$rdep")"
        [ -f "$OUT/$rbase" ] && install_name_tool -change "$rdep" "@loader_path/$rbase" "$f" || true
    done
done

# Drop machine-specific rpaths (the brew prefix and the build toolchain), so the
# only remaining search paths are @loader_path (the bundle) and the OS Swift
# runtime. Otherwise the brew rpath silently masks a missing lib on the builder.
for f in "$OUT/immich-ml-native" "$OUT"/*.dylib; do
    otool -l "$f" | awk '/cmd LC_RPATH/{r=1} r && /^[[:space:]]*path /{print $2; r=0}' | while IFS= read -r rp; do
        case "$rp" in
            /opt/homebrew*|*/Xcode.app/*|*.xctoolchain/*)
                install_name_tool -delete_rpath "$rp" "$f" 2>/dev/null || true ;;
        esac
    done
done

# re-sign ad-hoc (install_name_tool invalidates signatures) and VERIFY it took:
# a silently-broken signature fails to load on a user's machine just like a
# missing dep, so don't swallow the result.
for f in "$OUT/immich-ml-native" "$OUT"/*.dylib; do
    codesign -f -s - "$f" 2>/dev/null || { echo "ERROR: codesign failed on $(basename "$f")"; exit 1; }
    codesign --verify "$f" 2>/dev/null || { echo "ERROR: codesign --verify failed on $(basename "$f")"; exit 1; }
done

echo "bundle -> $OUT"
ls -la "$OUT"

# Fail the build unless the bundle is genuinely self-contained. The original
# check only looked at absolute /opt/homebrew load deps and missed the two ways
# #111 could still ship: a brew/toolchain LC_RPATH, and an @rpath load dep whose
# target is neither vendored nor an OS library. Check all three.
echo "verifying self-contained..."
leak=0
for f in "$OUT/immich-ml-native" "$OUT"/*.dylib; do
    bn="$(basename "$f")"
    if otool -L "$f" | awk 'NR>1 {print $1}' | grep -q '^/opt/homebrew'; then
        echo "  LEAK ($bn): /opt/homebrew load dep"; leak=1
    fi
    if otool -l "$f" | awk '/cmd LC_RPATH/{r=1} r && /^[[:space:]]*path /{print $2; r=0}' \
         | grep -qE '^/opt/homebrew|/Xcode.app/'; then
        echo "  LEAK ($bn): machine-specific LC_RPATH"; leak=1
    fi
    while IFS= read -r rdep; do
        rbase="$(basename "$rdep")"
        [ -f "$OUT/$rbase" ] && continue           # vendored beside the binary
        [ -f "/usr/lib/swift/$rbase" ] && continue # OS Swift runtime (on disk)
        case "$rbase" in libswift*) continue ;; esac # OS Swift runtime (dyld cache)
        echo "  LEAK ($bn): unresolved @rpath dep $rbase"; leak=1
    done < <(otool -L "$f" | awk 'NR>1 && $1 ~ /^@rpath\/.*\.dylib$/ {print $1}')
done
[ "$leak" = 0 ] || { echo "ERROR: bundle is not self-contained"; exit 1; }
echo "OK: self-contained ($(ls "$OUT"/*.dylib | wc -l | tr -d ' ') vendored dylibs; no brew load deps, no brew/toolchain rpaths, no unresolved @rpath deps)"
