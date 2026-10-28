#!/usr/bin/env bash
# Build "Immich Accelerator Service.app", the program the Homebrew service runs
# (see main.swift for why it exists), and package it as a release asset that
# the formula installs into libexec.
#
# Signed with the project's own certificate in a release (MACOS_SIGN_ID, a
# SHA-1 or name, and MACOS_SIGN_KEYCHAIN, see update-homebrew.yml); unset is
# ad-hoc, which runs fine but loses the point, since macOS then forgets the
# user's Allow on every rebuild.
#
# The bundle identifier must never change: it is what macOS remembers the
# user's permission by.
#
# Usage: build.sh <version>
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${1:?usage: build.sh <version>}"
APP="Immich Accelerator Service.app"
TARBALL="$PWD/immich-accelerator-service-${VERSION}.tar.gz"
SIGN_ID="${MACOS_SIGN_ID:--}"
sign_flags=(--force --sign "$SIGN_ID")
if [ "$SIGN_ID" != "-" ]; then
    # No --timestamp: Apple's timestamp service only countersigns its own
    # certificates, and nothing here is notarized.
    sign_flags+=(--options runtime)
fi
if [ -n "${MACOS_SIGN_KEYCHAIN:-}" ]; then
    sign_flags+=(--keychain "$MACOS_SIGN_KEYCHAIN")
fi

# One top-level folder around the bundle: Homebrew steps into a lone folder
# when it unpacks a resource, which would otherwise leave it inside the .app.
STAGE="$(mktemp -d)/immich-accelerator-service"
mkdir -p "$STAGE/$APP/Contents/MacOS"
swiftc -O -target arm64-apple-macos13 main.swift \
    -o "$STAGE/$APP/Contents/MacOS/immich-accelerator-service"
cat > "$STAGE/$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>com.epheterson.immich-accelerator.service</string>
    <key>CFBundleName</key><string>Immich Accelerator Service</string>
    <key>CFBundleDisplayName</key><string>Immich Accelerator Service</string>
    <key>CFBundleExecutable</key><string>immich-accelerator-service</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleVersion</key><string>${VERSION}</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSBackgroundOnly</key><true/>
    <key>NSNetworkVolumesUsageDescription</key><string>Immich Accelerator reads and writes your photo library on this network volume.</string>
    <key>NSLocalNetworkUsageDescription</key><string>Immich Accelerator connects to Immich's database and cache on your network.</string>
</dict>
</plist>
PLIST
codesign "${sign_flags[@]}" "$STAGE/$APP"
codesign --verify --strict "$STAGE/$APP"
codesign -dvv "$STAGE/$APP" 2>&1 | grep -E '^(Identifier|Authority|TeamIdentifier)=' || true

tar -czf "$TARBALL" -C "$(dirname "$STAGE")" "$(basename "$STAGE")"
echo "tarball : $TARBALL"
echo "sha256  : $(shasum -a 256 "$TARBALL" | cut -d' ' -f1)"
