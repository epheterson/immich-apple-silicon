import Foundation
import CoreGraphics

// Dispatch a /predict request across requested tasks and assemble Immich's
// response dict. Mirrors ml/src/main.py _process_predict: clip (visual|textual),
// facial-recognition, ocr; omits fields that weren't requested.
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
            throw PredictError(status: "400 Bad Request", message: "Invalid image")
        }
        cg = img
        let (buf, w, h) = rgbBuffer(img)
        rgb = buf; W = w; H = h
        response["imageWidth"] = W
        response["imageHeight"] = H
    }

    for (taskType, cfgAny) in entries {
        let cfg = cfgAny as? [String: Any] ?? [:]
        switch taskType {
        case "clip":
            if cfg["visual"] != nil, let cg = cg {
                try requireDefaultModel(cfg["visual"])
                response["clip"] = pyListString(models.clipVisual.embed(cg))
            } else if cfg["textual"] != nil, let t = text {
                try requireDefaultModel(cfg["textual"])
                response["clip"] = pyListString(models.clipText.encode(t))
            }

        case "facial-recognition":
            guard let data = imageData, let rgb = rgb, let ort = models.arcface else { break }
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
            response["facial-recognition"] = out

        case "ocr":
            guard let data = imageData else { break }
            let det = optDouble(cfg, "detection", "minScore") ?? 0.0
            let rec = optDouble(cfg, "recognition", "minScore") ?? 0.0
            response["ocr"] = recognizeTextImmich(imageData: data, minScore: Float(max(det, rec)),
                                                  languageCorrection: true)

        default:
            break
        }
    }
    return response
}

// v1 CLIP scope: the loaded ViT-B-32 default. A different requested modelName
// would need its own weights (plan Task 11); fail loudly rather than mis-embed.
private func requireDefaultModel(_ taskCfg: Any?) throws {
    guard let c = taskCfg as? [String: Any], let name = c["modelName"] as? String else { return }
    let normalized = name.replacingOccurrences(of: "::", with: "__")
    let ok = ["ViT-B-32__openai", "default"]
    if !ok.contains(normalized) {
        throw PredictError(status: "422 Unprocessable Entity",
                           message: "native engine v1 supports ViT-B-32__openai; requested \(name)")
    }
}

private func optDouble(_ cfg: [String: Any], _ section: String, _ key: String) -> Double? {
    ((cfg[section] as? [String: Any])?["options"] as? [String: Any])?[key] as? Double
}
