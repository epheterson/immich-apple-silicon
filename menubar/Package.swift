// swift-tools-version:5.9
import PackageDescription

// Immich Accelerator menu-bar app. Built by scripts/build_app.sh into an
// ad-hoc-signed "Immich Accelerator.app" (LSUIElement) and shipped via brew,
// same pattern as the native ML bundle. See docs/plans/2026-07-17-menubar-app.md.
let package = Package(
    name: "AcceleratorBar",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "AcceleratorBar")
    ]
)
