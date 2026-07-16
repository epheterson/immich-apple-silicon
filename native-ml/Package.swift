// swift-tools-version:5.9
import PackageDescription

// Native Swift ML service — drop-in replacement for the Python ML venv.
// See docs/plans/2026-07-16-native-swift-ml-service.md for the full plan.
// This is the validated compute seed (CLIP visual+text, face align, Vision OCR
// + detect, HTTP server); ORTSession (onnxruntime C ABI), full /predict
// dispatch, and packaging are in progress per the plan.
let package = Package(
    name: "immich-ml-native",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/ml-explore/mlx-swift", from: "0.21.0")
    ],
    targets: [
        .executableTarget(
            name: "immich-ml-native",
            dependencies: [
                .product(name: "MLX", package: "mlx-swift"),
                .product(name: "MLXNN", package: "mlx-swift"),
                .product(name: "MLXFast", package: "mlx-swift"),
            ]
        )
    ]
)
