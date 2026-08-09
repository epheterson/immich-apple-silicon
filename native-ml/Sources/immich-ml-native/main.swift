import Foundation
import MLX

// Native Swift ML service prototype: CLIP (mlx-swift) + OCR + face-detect (Vision).
// Proves the whole ML compute layer can be native — no Python, no venv, no torch.

MLX.Device.setDefault(device: Device(.cpu))   // CPU backend; GPU also works with mlx.metallib present

// --- CLIP text parity harness: encode phrases, dump ids + embeddings for cosine check ---
if CommandLine.arguments.contains("texttest") {
    let dir = "/tmp/mlx032test/clipmodel"
    let tk = CLIPTokenizer(modelDir: dir)
    let te = CLIPText(modelDir: dir, tokenizer: tk)
    let phrases = try! JSONSerialization.jsonObject(
        with: Data(contentsOf: URL(fileURLWithPath: "/tmp/clip_text_phrases.json"))) as! [String]
    var out = Data()
    for p in phrases {
        print("IDS \(p): \(tk.encode(p))")
        let e = te.encode(p)
        e.withUnsafeBytes { out.append(contentsOf: $0) }
    }
    try! out.write(to: URL(fileURLWithPath: "/tmp/clip_text_swift.f32"))
    print("wrote \(phrases.count) text embeddings")
    exit(0)
}

// --- Zoo parity harness: run ONNX zoo models, dump embeddings for oracle comparison ---
if CommandLine.arguments.contains("zootest") {
    let phrases = ["a photo of a cat", "sunset over the mountains", "OCR Test 123",
                   "A DOG, running!  on the beach?", "immich native swift"]
    let imagePath = ProcessInfo.processInfo.environment["ZOOTEST_IMAGE"] ?? "/tmp/face_test.jpg"
    let imageTag = (imagePath as NSString).lastPathComponent
    let defaultModels = ["ViT-B-16-SigLIP__webli", "ViT-B-16-SigLIP2__webli",
                          "ViT-L-16-SigLIP2-256__webli", "ViT-SO400M-16-SigLIP2-384__webli"]
    let models = ProcessInfo.processInfo.environment["ZOOTEST_MODELS"]
        .map { $0.split(separator: ",").map(String.init) } ?? defaultModels
    for name in models {
        do {
            let zoo = try ZooCLIP(name: name)
            guard let cg = loadCGImage(imagePath) else { fatalError("no image at \(imagePath)") }
            let ve = try zoo.embedVisual(cg)
            ve.withUnsafeBytes {
                try? Data($0).write(to: URL(fileURLWithPath: "/tmp/zoo_swift_\(name)_\(imageTag)_visual.f32"))
            }
            var tout = Data()
            for p in phrases {
                let te = try zoo.embedTextual(p)
                te.withUnsafeBytes { tout.append(contentsOf: $0) }
            }
            try tout.write(to: URL(fileURLWithPath: "/tmp/zoo_swift_\(name)_textual.f32"))
            print("\(name): visual dim=\(ve.count) first3=\(ve.prefix(3).map { String(format: "%.5f", $0) }); textual \(phrases.count) phrases dim=\(zoo.embedDim)")
        } catch {
            print("\(name): FAILED \(error)")
        }
    }
    exit(0)
}

// --- Latency benchmark: raw embedVisual/embedTextual compute time, no HTTP ---
// Used by scripts/native-ml-siglip-benchmark.py to compare the native mlx-swift
// path against the onnxruntime path it replaced. Run this binary twice — once
// normally (native) and once with ZOOCLIP_FORCE_ONNX=1 (see ZooCLIP.swift) — so
// both numbers come from the exact same harness and preprocessing code, only
// the inference backend differs.
if CommandLine.arguments.contains("benchtest") {
    let iters = Int(ProcessInfo.processInfo.environment["BENCH_ITERS"] ?? "") ?? 10
    let warmup = Int(ProcessInfo.processInfo.environment["BENCH_WARMUP"] ?? "") ?? 3
    let imagePath = ProcessInfo.processInfo.environment["BENCH_IMAGE"] ?? "/tmp/face_test.jpg"
    let phrase = "a photo of a cat"
    let defaultModels = ["ViT-B-16-SigLIP__webli", "ViT-B-16-SigLIP2__webli",
                          "ViT-L-16-SigLIP2-256__webli", "ViT-SO400M-16-SigLIP2-384__webli"]
    let models = ProcessInfo.processInfo.environment["BENCH_MODELS"]
        .map { $0.split(separator: ",").map(String.init) } ?? defaultModels

    func timeMs(_ n: Int, _ body: () throws -> Void) rethrows -> [Double] {
        var samples: [Double] = []
        samples.reserveCapacity(n)
        for _ in 0..<n {
            let start = DispatchTime.now()
            try body()
            let end = DispatchTime.now()
            samples.append(Double(end.uptimeNanoseconds - start.uptimeNanoseconds) / 1_000_000)
        }
        return samples
    }
    func median(_ xs: [Double]) -> Double {
        let s = xs.sorted()
        return s.count % 2 == 1 ? s[s.count / 2] : (s[s.count / 2 - 1] + s[s.count / 2]) / 2
    }

    for name in models {
        do {
            let zoo = try ZooCLIP(name: name)
            guard let cg = loadCGImage(imagePath) else { fatalError("no image at \(imagePath)") }
            _ = try timeMs(warmup) { _ = try zoo.embedVisual(cg) }
            let visual = try timeMs(iters) { _ = try zoo.embedVisual(cg) }
            _ = try timeMs(warmup) { _ = try zoo.embedTextual(phrase) }
            let textual = try timeMs(iters) { _ = try zoo.embedTextual(phrase) }
            print("BENCH \(name) visual_ms=\(median(visual)) textual_ms=\(median(textual)) n=\(iters)")
        } catch {
            print("BENCH \(name) FAILED \(error)")
        }
    }
    exit(0)
}

// --- Face-align parity harness: align /tmp/face_test.jpg with Python's landmarks ---
if CommandLine.arguments.contains("aligntest") {
    guard let cg = loadCGImage("/tmp/face_test.jpg") else { fatalError("no face image") }
    let (rgb, w, h) = rgbBuffer(cg)
    let raw = try! JSONSerialization.jsonObject(
        with: Data(contentsOf: URL(fileURLWithPath: "/tmp/face_landmarks.json"))) as! [[Any]]
    let lm = raw.map { $0.map { ($0 as! NSNumber).doubleValue } }
    let aligned = FaceAlign.normCrop(rgb: rgb, w: w, h: h, landmarks: lm)
    try! Data(aligned).write(to: URL(fileURLWithPath: "/tmp/face_aligned_swift.rgb"))
    print("wrote aligned 112x112 rgb (img \(w)x\(h), \(lm.count) landmarks)")
    exit(0)
}

// --- Same-pixel align isolation: align cv2-decoded source, so only warp math differs ---
if CommandLine.arguments.contains("aligntest2") {
    let dims = try! JSONSerialization.jsonObject(
        with: Data(contentsOf: URL(fileURLWithPath: "/tmp/face_src_dims.json"))) as! [Any]
    let w = (dims[0] as! NSNumber).intValue, h = (dims[1] as! NSNumber).intValue
    let rgb = [UInt8](try! Data(contentsOf: URL(fileURLWithPath: "/tmp/face_src_rgb.bin")))
    let raw = try! JSONSerialization.jsonObject(
        with: Data(contentsOf: URL(fileURLWithPath: "/tmp/face_landmarks.json"))) as! [[Any]]
    let lm = raw.map { $0.map { ($0 as! NSNumber).doubleValue } }
    let aligned = FaceAlign.normCrop(rgb: rgb, w: w, h: h, landmarks: lm)
    try! Data(aligned).write(to: URL(fileURLWithPath: "/tmp/face_aligned_swift2.rgb"))
    print("wrote aligned from cv2-decoded source (\(w)x\(h))")
    exit(0)
}

// --- Full in-process native face pipeline: Vision detect -> align -> ArcFace (onnxruntime) ---
if CommandLine.arguments.contains("facetest") {
    let modelPath = NSHomeDirectory() + "/.insightface/models/buffalo_l/w600k_r50.onnx"
    guard let ort = ORTSession(modelPath: modelPath) else { fatalError("ORT load failed: \(modelPath)") }
    guard let cg = loadCGImage("/tmp/face_test.jpg") else { fatalError("no face image") }
    let (rgb, w, h) = rgbBuffer(cg)
    let imageData = try! Data(contentsOf: URL(fileURLWithPath: "/tmp/face_test.jpg"))
    let faces = detectFacesWithLandmarks(imageData: imageData, width: w, height: h)
    let f0 = faces.first
    print("detected \(faces.count) face(s); box=(\(f0?.x1 ?? -1),\(f0?.y1 ?? -1))-(\(f0?.x2 ?? -1),\(f0?.y2 ?? -1)) score=\(f0.map { String(format: "%.3f", $0.score) } ?? "-")")
    if let lm = f0?.landmarks { print("landmarks=\(lm.map { $0.map { ($0 * 100).rounded() / 100 } })") }
    let embs = embedFaces(srcRGB: rgb, w: w, h: h, faces: faces, model: ort)
    if let e = embs.first ?? nil {
        e.withUnsafeBytes { try? Data($0).write(to: URL(fileURLWithPath: "/tmp/face_emb_swift.f32")) }
        print("wrote 512-d embedding; first5=\(e.prefix(5).map { String(format: "%.5f", $0) })")
    } else { print("no embedding produced") }
    exit(0)
}

// --- Debug: dump Resize.bicubic's raw output for pixel-level comparison against PIL ---
if CommandLine.arguments.contains("resizetest") {
    guard let cg = loadCGImage("/tmp/face_test.jpg") else { fatalError("no image") }
    let (rgb, w, h) = rgbBuffer(cg)
    let size = CommandLine.arguments.last.flatMap { Int($0) } ?? 384
    let resized = Resize.bicubic(rgb, w: w, h: h, outW: size, outH: size)
    try! Data(resized).write(to: URL(fileURLWithPath: "/tmp/swift_resized_\(size).raw"))
    print("wrote \(resized.count) bytes")
    exit(0)
}

let MODEL = ProcessInfo.processInfo.environment["ML_CLIP_DIR"] ?? "/tmp/mlx032test/clipmodel"
let ARCFACE = ProcessInfo.processInfo.environment["ML_ARCFACE"]
    ?? (NSHomeDirectory() + "/.insightface/models/buffalo_l/w600k_r50.onnx")

// --- Serve the full /predict contract (CLIP visual+text, faces, OCR) ---
if CommandLine.arguments.contains("serve") {
    let port = UInt16(CommandLine.arguments.last.flatMap { UInt16($0) } ?? 3999)
    let models = Models(clipDir: MODEL, arcfacePath: ARCFACE)
    print("[native-ml] models loaded (clip=\(MODEL), arcface=\(models.arcface != nil ? "ok" : "MISSING"))")
    print("[native-ml] serving on :\(port) (/ /ping /health /predict)")
    startServer(port: port, models: models)
    dispatchMain()
}

let clip = CLIPEncoder(modelDir: MODEL)
print("[native-ml] CLIP model loaded from \(MODEL)")

func selfTest() {
    if let cg = loadCGImage("/tmp/clip_testimg.png") {
        let e = clip.embed(cg)
        let l2 = sqrt(e.map { $0 * $0 }.reduce(0, +))
        print("CLIP  : dim=\(e.count) L2=\(String(format: "%.5f", l2)) first3=\(e.prefix(3).map { String(format: "%.5f", $0) })")
        let d = e.withUnsafeBytes { Data($0) }
        try? d.write(to: URL(fileURLWithPath: "/tmp/clip_swift_emb.bin"))
    }
    if let cg = loadCGImage("/tmp/ocr_test.png") {
        let lines = runOCR(cg)
        print("OCR   : \(lines.count) lines -> \"\(lines.map { $0.text }.joined(separator: " | "))\"")
    } else { print("OCR   : (no /tmp/ocr_test.png)") }
    if let cg = loadCGImage("/tmp/face_test.jpg") {
        let f = detectFaces(cg)
        print("FACE  : \(f.count) detected, conf=\(f.map { String(format: "%.2f", $0.confidence) })")
    } else { print("FACE  : (no /tmp/face_test.jpg)") }
}

selfTest()
