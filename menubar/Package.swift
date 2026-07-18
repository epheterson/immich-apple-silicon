// swift-tools-version:5.9
import PackageDescription

// Immich Accelerator menu-bar app. Built by scripts/build_app.sh into an
// ad-hoc-signed "Immich Accelerator.app" (LSUIElement) and shipped via brew,
// same pattern as the native ML bundle. See docs/plans/2026-07-17-menubar-app.md.
let package = Package(
    name: "AcceleratorBar",
    platforms: [.macOS(.v14)],
    dependencies: [
        // Sparkle in-app auto-updates. The app is Developer-ID signed + notarized
        // in CI so Sparkle's downloaded updates clear Gatekeeper.
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.8.0")
    ],
    targets: [
        .executableTarget(
            name: "AcceleratorBar",
            dependencies: [.product(name: "Sparkle", package: "Sparkle")]
        )
    ]
)
