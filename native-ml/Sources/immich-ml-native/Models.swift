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
    private var zooLoading: String?           // model name currently downloading/loading
    private let zooCond = NSCondition()

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

    // Zoo model for a non-default name, switching if a different model is
    // requested. First use downloads the model (minutes for large towers), so
    // the lock is NOT held during download/load: concurrent requests for the
    // same model wait on the condition; the resident model keeps serving and is
    // only replaced once the new one loaded successfully (a failed load, e.g. a
    // typo'd name or HF outage, must never evict a healthy model).
    func zoo(for name: String) throws -> ZooCLIP {
        zooCond.lock()
        while true {
            if let z = zooModel, z.name == name {
                zooCond.unlock()
                return z
            }
            if zooLoading == nil { break }       // no load in flight: we load
            if zooLoading == name {
                zooCond.wait()                   // same model loading: wait for it
                continue
            }
            // A different model is loading; wait rather than downloading two
            // multi-GB models at once.
            zooCond.wait()
        }
        zooLoading = name
        zooCond.unlock()

        var loaded: ZooCLIP?
        var failure: Error?
        do { loaded = try ZooCLIP(name: name) } catch { failure = error }

        zooCond.lock()
        zooLoading = nil
        if let z = loaded {
            if let old = zooModel, old.name != name {
                print("[native-ml] switched zoo model \(old.name) -> \(name)")
            }
            zooModel = z
        }
        zooCond.broadcast()
        zooCond.unlock()

        if let z = loaded { return z }
        throw failure ?? PredictError(status: "500 Internal Server Error", message: "zoo load failed")
    }
}

// Immich wire format: an embedding is a stringified Python list, e.g. "[0.1, 0.2]".
// Immich parses it with json.loads, so any valid JSON-number repr round-trips.
func pyListString(_ e: [Float]) -> String {
    "[" + e.map { String($0) }.joined(separator: ", ") + "]"
}
