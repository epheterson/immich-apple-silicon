import Foundation

// Model registry. The default ViT-B-32 loads at startup on the mlx fast path
// (bit-identical to the reference, proven). Any other CLIP model Immich requests
// resolves through the ONNX zoo (downloaded on demand, cached in ~/.cache),
// with Python-service switch semantics: one zoo model resident at a time.
final class Models {
    static let defaultClip = "ViT-B-32__openai"

    let clipDir: String
    let clipVisual: CLIPEncoder
    let clipText: CLIPText
    let arcface: ORTSession?

    private var zooModel: ZooCLIP?
    private let zooLock = NSLock()

    init(clipDir: String, arcfacePath: String) {
        self.clipDir = clipDir
        clipVisual = CLIPEncoder(modelDir: clipDir)
        clipText = CLIPText(modelDir: clipDir, tokenizer: CLIPTokenizer(modelDir: clipDir))
        arcface = ORTSession(modelPath: arcfacePath)
    }

    // Normalize an Immich model name the way the Python service does.
    static func normalize(_ name: String) -> String {
        var n = name.replacingOccurrences(of: "::", with: "__")
        if let last = n.split(separator: "/").last { n = String(last) }
        return n == "default" ? defaultClip : n
    }

    // Zoo model for a non-default name, switching (and releasing) the previous
    // one if a different model is requested. First use downloads the model.
    func zoo(for name: String) throws -> ZooCLIP {
        zooLock.lock()
        defer { zooLock.unlock() }
        if let z = zooModel, z.name == name { return z }
        if zooModel != nil {
            print("[native-ml] switching zoo model \(zooModel!.name) -> \(name)")
        }
        zooModel = nil   // release before loading the next (memory)
        let z = try ZooCLIP(name: name)
        zooModel = z
        return z
    }
}

// Immich wire format: an embedding is a stringified Python list, e.g. "[0.1, 0.2]".
// Immich parses it with json.loads, so any valid JSON-number repr round-trips.
func pyListString(_ e: [Float]) -> String {
    "[" + e.map { String($0) }.joined(separator: ", ") + "]"
}
