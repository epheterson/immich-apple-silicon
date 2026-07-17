import Foundation

// Full native face embedding: align (norm_crop or bbox fallback) -> ArcFace
// input normalization (matches cv2.dnn.blobFromImages: (rgb-127.5)/127.5, NCHW)
// -> onnxruntime -> L2-normalize. Order-preserving per face.
func embedFaces(srcRGB: [UInt8], w: Int, h: Int, faces: [DetectedFace], model: ORTSession) -> [[Float]?] {
    faces.map { face in
        let aligned: [UInt8]
        if let lm = face.landmarks {
            aligned = FaceAlign.normCrop(rgb: srcRGB, w: w, h: h, landmarks: lm)
        } else if let crop = bboxCrop(srcRGB, w: w, h: h, face: face) {
            aligned = crop
        } else {
            return nil
        }
        var input = [Float](repeating: 0, count: 3 * 112 * 112)
        for y in 0..<112 {
            for x in 0..<112 {
                for c in 0..<3 {
                    input[c * 112 * 112 + y * 112 + x] = (Float(aligned[(y * 112 + x) * 3 + c]) - 127.5) / 127.5
                }
            }
        }
        guard let e = model.run(input, shape: [1, 3, 112, 112]) else { return nil }
        let norm = e.map { $0 * $0 }.reduce(0, +).squareRoot()
        return norm > 0 ? e.map { $0 / norm } : e
    }
}

// bbox 10%-pad crop + resize to 112 (landmark-less fallback, mirrors the Python path).
private func bboxCrop(_ rgb: [UInt8], w: Int, h: Int, face: DetectedFace) -> [UInt8]? {
    let bw = face.x2 - face.x1, bh = face.y2 - face.y1
    let px = bw / 10, py = bh / 10
    let x1 = max(0, face.x1 - px), y1 = max(0, face.y1 - py)
    let x2 = min(w, face.x2 + px), y2 = min(h, face.y2 + py)
    let cw = x2 - x1, ch = y2 - y1
    guard cw > 0, ch > 0 else { return nil }
    var out = [UInt8](repeating: 0, count: 112 * 112 * 3)
    for oy in 0..<112 {
        for ox in 0..<112 {
            let sx = x1 + ox * cw / 112, sy = y1 + oy * ch / 112
            for c in 0..<3 { out[(oy * 112 + ox) * 3 + c] = rgb[(sy * w + sx) * 3 + c] }
        }
    }
    return out
}
