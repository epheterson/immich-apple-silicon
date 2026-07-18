#!/usr/bin/env bash
# Build "Immich Accelerator.app" from the SwiftPM executable: release build,
# minimal LSUIElement bundle, ad-hoc signed. Ships via brew like the ML bundle
# (brew does not quarantine, so no notarization needed).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/homebrew/bin:$PATH

VERSION="${1:-dev}"
APP="${2:-Immich Accelerator.app}"

swift build -c release >/dev/null
echo "built AcceleratorBar"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/AcceleratorBar "$APP/Contents/MacOS/AcceleratorBar"

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
</dict>
</plist>
PLIST

codesign -f -s - "$APP" >/dev/null 2>&1 || true
echo "app -> $APP ($(du -sh "$APP" | cut -f1))"
codesign -v "$APP" && echo "signature ok"
