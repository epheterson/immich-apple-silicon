#!/usr/bin/env bash
# Package the relocatable native-ml bundle as a versioned tarball + sha256 for a
# GitHub release asset. Homebrew installs it as `resource "native_ml"` (no
# notarization needed — brew does not quarantine its downloads, and the bundle is
# ad-hoc signed so it runs on any Apple Silicon Mac). Run on Apple Silicon with
# the Swift + Metal toolchain.
#
# Usage: release_bundle.sh <version>   (e.g. 1.6.0)
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/homebrew/bin:$PATH

VERSION="${1:?usage: release_bundle.sh <version>}"
STAGE="$(mktemp -d)/immich-ml-native"
TARBALL="$PWD/immich-ml-native-${VERSION}-macos-arm64.tar.gz"

bash scripts/build_bundle.sh "$STAGE" >/dev/null
tar -czf "$TARBALL" -C "$(dirname "$STAGE")" "$(basename "$STAGE")"

SHA=$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)
SIZE=$(du -h "$TARBALL" | cut -f1)
echo "tarball : $TARBALL ($SIZE)"
echo "sha256  : $SHA"
echo
echo "Upload as a release asset, then set the native_ml resource url + sha256 in the formula."
