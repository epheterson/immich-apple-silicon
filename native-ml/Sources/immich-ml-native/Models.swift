import Foundation

// Models loaded once at startup. v1 targets the Python service's defaults
// (ViT-B-32 CLIP, buffalo_l ArcFace) — the models Immich requests by default and
// that Eric's prod uses. Model-switching for other CLIP archs is a follow-up
// (plan Task 11); unknown models return a clear error rather than a wrong vector.
final class Models {
    let clipDir: String
    let clipVisual: CLIPEncoder
    let clipText: CLIPText
    let arcface: ORTSession?

    init(clipDir: String, arcfacePath: String) {
        self.clipDir = clipDir
        clipVisual = CLIPEncoder(modelDir: clipDir)
        clipText = CLIPText(modelDir: clipDir, tokenizer: CLIPTokenizer(modelDir: clipDir))
        arcface = ORTSession(modelPath: arcfacePath)
    }
}

// Immich wire format: an embedding is a stringified Python list, e.g. "[0.1, 0.2]".
// Immich parses it with json.loads, so any valid JSON-number repr round-trips.
func pyListString(_ e: [Float]) -> String {
    "[" + e.map { String($0) }.joined(separator: ", ") + "]"
}
