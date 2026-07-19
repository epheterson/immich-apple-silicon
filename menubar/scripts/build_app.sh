#!/usr/bin/env bash
# Build "Immich Accelerator.app" from the SwiftPM executable and embed Sparkle
# for in-app auto-updates.
#
# Two signing modes, selected by the MACOS_SIGN_ID env var:
#   - ad-hoc (default, MACOS_SIGN_ID unset or "-"): local/dev builds. The cask's
#     postflight strips quarantine so it still launches.
#   - Developer ID (MACOS_SIGN_ID="Developer ID Application: ..."): CI builds that
#     will be notarized + stapled. Hardened runtime + secure timestamp are added
#     so notarytool accepts the bundle, and Sparkle's downloaded updates clear
#     Gatekeeper. Requires an unlocked keychain holding that identity.
#
# Sparkle wiring: SUFeedURL + SUPublicEDKey go in Info.plist. The public EdDSA
# key (not secret) is baked in via SPARKLE_PUBLIC_ED_KEY; the matching private
# key signs the appcast in CI. An empty key just disables updates (fine for dev).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/homebrew/bin:$PATH

VERSION="${1:-dev}"
APP="${2:-Immich Accelerator.app}"

SIGN_ID="${MACOS_SIGN_ID:--}"
# App-scoped Sparkle public EdDSA key (not secret; verifies the appcast). Baked
# in as the default so even local/dev builds ship a verifying Info.plist; CI may
# override via the SPARKLE_PUBLIC_ED_KEY env for key rotation.
SPARKLE_PUBLIC_ED_KEY="${SPARKLE_PUBLIC_ED_KEY:-P8DlelneVjoU1Uio5uhiHA5d6uqDlgPkxhAhA3dcZqY=}"
SUFEED_URL="${SUFEED_URL:-https://github.com/epheterson/immich-apple-silicon/releases/latest/download/appcast.xml}"

# codesign flags: a real Developer ID identity gets hardened runtime + a secure
# timestamp (both required for notarization); ad-hoc ("-") supports neither.
sign_flags=(--force --sign "$SIGN_ID")
if [ "$SIGN_ID" != "-" ]; then
    sign_flags+=(--options runtime --timestamp)
fi
sign() { codesign "${sign_flags[@]}" "$@"; }

swift build -c release >/dev/null
echo "built AcceleratorBar"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$APP/Contents/Frameworks"
cp .build/release/AcceleratorBar "$APP/Contents/MacOS/AcceleratorBar"

# Embed Sparkle.framework. XPCServices are only needed by sandboxed apps; this
# menu-bar app is not sandboxed, so drop them (fewer things to sign/notarize,
# and they can otherwise fail to launch from a relocated bundle).
cp -R .build/release/Sparkle.framework "$APP/Contents/Frameworks/"
rm -rf "$APP/Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices"

# The SwiftPM binary loads @rpath/Sparkle.framework/... but only has rpaths for
# the Swift runtime + a machine-specific Xcode toolchain path. Point it at the
# embedded framework and drop the leaked toolchain rpath.
install_name_tool -add_rpath "@executable_path/../Frameworks" "$APP/Contents/MacOS/AcceleratorBar"
TOOLCHAIN_RPATH="$(otool -l "$APP/Contents/MacOS/AcceleratorBar" \
    | awk '/LC_RPATH/{f=1} f&&/path /{print $2; f=0}' \
    | grep -E '/Xcode\.app/|\.xctoolchain/' || true)"
for rp in $TOOLCHAIN_RPATH; do
    install_name_tool -delete_rpath "$rp" "$APP/Contents/MacOS/AcceleratorBar" 2>/dev/null || true
done

# App icon: generate once (scripts/make_icon.swift), reuse if present.
if [ ! -f AppIcon.icns ]; then
    (cd scripts 2>/dev/null && swift make_icon.swift && iconutil -c icns AppIcon.iconset -o ../AppIcon.icns && rm -rf AppIcon.iconset) || true
fi
[ -f AppIcon.icns ] && cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>AcceleratorBar</string>
    <key>CFBundleIdentifier</key><string>com.epheterson.immich-accelerator.menubar</string>
    <key>CFBundleName</key><string>Immich Accelerator</string>
    <key>CFBundleDisplayName</key><string>Immich Accelerator</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>${VERSION}</string>
    <key>CFBundleVersion</key><string>${VERSION}</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
    <key>SUFeedURL</key><string>${SUFEED_URL}</string>
    <key>SUPublicEDKey</key><string>${SPARKLE_PUBLIC_ED_KEY}</string>
    <key>SUEnableAutomaticChecks</key><true/>
    <key>SUScheduledCheckInterval</key><integer>86400</integer>
</dict>
</plist>
PLIST

# Sign inside-out: nested Sparkle helpers first, then the framework, then the
# main executable, then the whole app.
SPARKLE="$APP/Contents/Frameworks/Sparkle.framework"
find "$SPARKLE" -type f \( -name "*.xpc" -o -name "Autoupdate" -o -name "Updater" \) \
    -exec codesign "${sign_flags[@]}" {} \;
sign "$SPARKLE/Versions/B/Updater.app" 2>/dev/null || true
sign "$SPARKLE/Versions/B/Sparkle"
sign "$SPARKLE"
sign "$APP/Contents/MacOS/AcceleratorBar"
sign "$APP"

echo "app -> $APP ($(du -sh "$APP" | cut -f1)) [sign: $SIGN_ID]"
codesign -v --strict "$APP" && echo "signature ok"
