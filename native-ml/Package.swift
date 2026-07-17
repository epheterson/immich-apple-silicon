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
        .package(url: "https://github.com/ml-explore/mlx-swift", from: "0.21.0"),
        // tokenizer.json support for the ONNX model zoo (SigLIP sentencepiece etc.)
        .package(url: "https://github.com/huggingface/swift-transformers", from: "1.3.0"),
    ],
    targets: [
        // onnxruntime C shim (face embedding). Header/lib paths are Homebrew's
        // onnxruntime; the release bundle relinks with @loader_path (see plan Task 12).
        .target(
            name: "COnnxShim",
            cSettings: [
                .unsafeFlags(["-I/opt/homebrew/opt/onnxruntime/include/onnxruntime"])
            ]
        ),
        .executableTarget(
            name: "immich-ml-native",
            dependencies: [
                .product(name: "MLX", package: "mlx-swift"),
                .product(name: "MLXNN", package: "mlx-swift"),
                .product(name: "MLXFast", package: "mlx-swift"),
                .product(name: "Tokenizers", package: "swift-transformers"),
                "COnnxShim",
            ],
            linkerSettings: [
                .unsafeFlags([
                    "-L/opt/homebrew/opt/onnxruntime/lib", "-lonnxruntime",
                    "-Xlinker", "-rpath", "-Xlinker", "/opt/homebrew/opt/onnxruntime/lib",
                ])
            ]
        ),
    ]
)
