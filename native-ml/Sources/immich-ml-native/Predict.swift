import Foundation
import CoreGraphics

// Dispatch a /predict request across requested tasks and assemble Immich's
// response dict. Mirrors ml/src/main.py's _process_predict both in behavior
// and in its [native-ml]-prefixed log output: same "  <task>: Nms" per-task
// lines and "predict: N task(s) [...] completed in Nms" summary, so `logs ml`
// reads the same regardless of which engine is running.
struct PredictError: Error { let status: String; let message: String }

func processPredict(entries: [String: Any], imageData: Data?, text: String?, models: Models) throws -> [String: Any] {
    if imageData == nil && text == nil {
        throw PredictError(status: "400 Bad Request", message: "Either image or text must be provided")
    }

    var response: [String: Any] = [:]
    var cg: CGImage?
    var rgb: [UInt8]?
    var W = 0, H = 0
    if let data = imageData {
        guard let img = loadCGImage(data: data) else {
            print("[native-ml] Failed to read/decode image")
            throw PredictError(status: "400 Bad Request", message: "Invalid image")
        }
        cg = img
        let (buf, w, h) = rgbBuffer(img)
        rgb = buf; W = w; H = h
        response["imageWidth"] = W
        response["imageHeight"] = H
    }

    // Requested task types, in a fixed order — mirrors python's
    // `[t for t in tasks.keys() if t in (...)]` used for the summary line.
    // Reflects what was requested, not what actually produced a result (same
    // as python: a task that silently no-ops still counts here).
    let requestedTaskNames = ["clip", "facial-recognition", "ocr"].filter { entries[$0] != nil }

    // Prints "  <name>: Nms" once `body` returns true (a result was produced),
    // matching python's `_timed` wrapper, which only logs `if result is not None`.
    func timed(_ name: String, _ body: () throws -> Bool) rethrows {
        let t0 = DispatchTime.now()
        let ran = try body()
        guard ran else { return }
        let ms = Double(DispatchTime.now().uptimeNanoseconds - t0.uptimeNanoseconds) / 1_000_000
        print("[native-ml]   \(name): \(String(format: "%.0f", ms))ms")
    }

    let overallStart = DispatchTime.now()

    if let cfgAny = entries["clip"] {
        let cfg = cfgAny as? [String: Any] ?? [:]
        try timed("clip") {
            if let vCfg = cfg["visual"] as? [String: Any], let cg = cg {
                let name = Models.normalize(vCfg["modelName"] as? String ?? Models.defaultClip)
                response["clip"] = name == Models.defaultClip
                    ? pyListString(models.clipVisual.embed(cg))
                    : pyListString(try models.zoo(for: name).embedVisual(cg))
                return true
            } else if let tCfg = cfg["textual"] as? [String: Any], let t = text {
                let name = Models.normalize(tCfg["modelName"] as? String ?? Models.defaultClip)
                response["clip"] = name == Models.defaultClip
                    ? pyListString(models.clipText.encode(t))
                    : pyListString(try models.zoo(for: name).embedTextual(t))
                return true
            }
            return false
        }
    }

    if let cfgAny = entries["facial-recognition"] {
        let cfg = cfgAny as? [String: Any] ?? [:]
        timed("faces") {
            guard let data = imageData, let rgb = rgb, let ort = models.arcface else { return false }
            let minScore = optDouble(cfg, "detection", "minScore") ?? 0.7
            let faces = detectFacesWithLandmarks(imageData: data, width: W, height: H)
                .filter { Double($0.score) >= minScore }
            let embs = embedFaces(srcRGB: rgb, w: W, h: H, faces: faces, model: ort)
            var out: [[String: Any]] = []
            for (f, e) in zip(faces, embs) {
                guard let e = e else { continue }
                out.append([
                    "boundingBox": ["x1": f.x1, "y1": f.y1, "x2": f.x2, "y2": f.y2],
                    "embedding": pyListString(e),
                    "score": Double(f.score),
                ])
            }
            print("[native-ml]   faces: \(out.count) detected")
            response["facial-recognition"] = out
            return true
        }
    }

    if let cfgAny = entries["ocr"] {
        let cfg = cfgAny as? [String: Any] ?? [:]
        timed("ocr") {
            guard let data = imageData else { return false }
            let det = optDouble(cfg, "detection", "minScore") ?? 0.0
            let rec = optDouble(cfg, "recognition", "minScore") ?? 0.0
            response["ocr"] = recognizeTextImmich(imageData: data, minScore: Float(max(det, rec)),
                                                  languageCorrection: true)
            return true
        }
    }

    let totalMs = Double(DispatchTime.now().uptimeNanoseconds - overallStart.uptimeNanoseconds) / 1_000_000
    print("[native-ml] predict: \(requestedTaskNames.count) task(s) "
        + "[\(requestedTaskNames.joined(separator: "+"))] completed in \(String(format: "%.0f", totalMs))ms")

    return response
}

private func optDouble(_ cfg: [String: Any], _ section: String, _ key: String) -> Double? {
    ((cfg[section] as? [String: Any])?["options"] as? [String: Any])?[key] as? Double
}
