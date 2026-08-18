import Foundation
import MLX

// Model registry. The default ViT-B-32 runs on the mlx fast path (bit-identical
// to the reference, proven). Any other CLIP model Immich requests resolves
// through the ONNX zoo (downloaded on demand, cached in ~/.cache), with
// Python-service switch semantics: one zoo model resident at a time.
//
// Every model here is loaded on first use and, when a TTL is set, released once
// it goes unused. Immich's own ML container gets that by exiting after
// MACHINE_LEARNING_MODEL_TTL and letting gunicorn fork a replacement; that is
// not available to a single process which owns its own listening socket, so the
// models are released in place instead. The Python service does the same.
final class Models {
    static let defaultClip = "ViT-B-32__openai"

    let clipDir: String
    let arcfacePath: String

    private var zooModel: ZooCLIP?
    private var zooLoading: String?           // model name currently downloading/loading
    private let zooCond = NSCondition()
    private var zooLastUsed = ContinuousClock.now

    // The default towers and ArcFace load in about a second from local disk, so
    // a plain lock is enough here. The zoo path needs its condition variable
    // because a first load can download gigabytes.
    private let localLock = NSLock()
    private var clipVisualModel: CLIPEncoder?
    private var clipTextModel: CLIPText?
    private var arcfaceSession: ORTSession?
    private var clipLastUsed = ContinuousClock.now
    private var arcfaceLastUsed = ContinuousClock.now

    private var idleTimer: DispatchSourceTimer?

    // Seconds a model may sit unused before it is released. The default matches
    // MACHINE_LEARNING_MODEL_TTL in Immich's own ML container. Set 0 to keep
    // every model resident for the life of the process.
    static let defaultTTL: TimeInterval = 300

    static var modelTTL: TimeInterval {
        guard let raw = ProcessInfo.processInfo.environment["IMMICH_ACCEL_ML_MODEL_TTL"] else {
            return defaultTTL
        }
        guard let seconds = TimeInterval(raw), seconds > 0 else { return 0 }
        return seconds
    }

    init(clipDir: String, arcfacePath: String) {
        self.clipDir = clipDir
        self.arcfacePath = arcfacePath
        startIdleTimer()
    }

    deinit { idleTimer?.cancel() }

    // Whether face recognition can run, which is a different question from
    // whether its session happens to be loaded right now.
    var arcfaceAvailable: Bool { FileManager.default.fileExists(atPath: arcfacePath) }

    func clipVisual() -> CLIPEncoder {
        localLock.lock()
        defer { localLock.unlock() }
        clipLastUsed = .now
        if let m = clipVisualModel { return m }
        let m = CLIPEncoder(modelDir: clipDir)
        clipVisualModel = m
        return m
    }

    func clipText() -> CLIPText {
        localLock.lock()
        defer { localLock.unlock() }
        clipLastUsed = .now
        if let m = clipTextModel { return m }
        let m = CLIPText(modelDir: clipDir, tokenizer: CLIPTokenizer(modelDir: clipDir))
        clipTextModel = m
        return m
    }

    func arcface() -> ORTSession? {
        localLock.lock()
        defer { localLock.unlock() }
        arcfaceLastUsed = .now
        if let s = arcfaceSession { return s }
        let s = ORTSession(modelPath: arcfacePath)
        arcfaceSession = s
        return s
    }

    private func startIdleTimer() {
        let ttl = Self.modelTTL
        guard ttl > 0 else { return }
        // Poll at most once a minute: eviction is a memory optimisation, not
        // something that has to land on the exact second the TTL expires.
        let period = min(ttl, 60)
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + period, repeating: period)
        timer.setEventHandler { [weak self] in self?.evictIdle(ttl: ttl) }
        timer.resume()
        idleTimer = timer
        print("[native-ml] idle model eviction enabled (TTL \(Int(ttl))s)")
    }

    // Each model type is timed separately, so a long CLIP backfill does not keep
    // the face model resident alongside it.
    //
    // Dropping a reference is all this does. A request that already took a model
    // out of one of the accessors above keeps it alive through ARC until it is
    // finished, so eviction never races an inference — the worst case is a
    // reload on the next request.
    private func evictIdle(ttl: TimeInterval) {
        let cutoff = Duration.seconds(ttl)
        var released: [String] = []

        zooCond.lock()
        if zooLoading == nil, let z = zooModel, zooLastUsed.duration(to: .now) >= cutoff {
            zooModel = nil
            released.append(z.name)
        }
        zooCond.unlock()

        localLock.lock()
        if clipVisualModel != nil || clipTextModel != nil, clipLastUsed.duration(to: .now) >= cutoff {
            clipVisualModel = nil
            clipTextModel = nil
            released.append(Self.defaultClip)
        }
        if arcfaceSession != nil, arcfaceLastUsed.duration(to: .now) >= cutoff {
            arcfaceSession = nil
            released.append("arcface")
        }
        localLock.unlock()

        guard !released.isEmpty else { return }
        // Releasing the arrays is not enough on its own: MLX keeps freed device
        // buffers in its own cache, so the memory only goes back to the OS once
        // that cache is dropped too.
        MLX.Memory.clearCache()
        print("[native-ml] released idle models after \(Int(ttl))s: \(released.joined(separator: ", "))")
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
                zooLastUsed = .now
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
            zooLastUsed = .now
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
