import CoreGraphics
import Foundation
import Tokenizers

// Native ML cache root (models persist across upgrades).
let NATIVE_CACHE_DIR = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".cache/immich-ml-native")

// CLIP model zoo: runs Immich's own ONNX exports (huggingface.co/immich-app/*)
// through onnxruntime, replicating immich_ml's exact preprocessing, so any model
// a user selects in Immich produces the same embeddings as the Docker service.
// The default ViT-B-32 keeps the mlx fast path; everything else lands here.
// Architecture insight (ONNX zoo + ORT natively) credit: lucka-me/michina-swift.
final class ZooCLIP {
    struct PreprocessCfg: Decodable {
        let size: SizeValue
        let mean: [Float]
        let std: [Float]

        enum SizeValue: Decodable {
            case int(Int), list([Int])
            init(from d: Swift.Decoder) throws {  // Tokenizers exports its own Decoder
                let c = try d.singleValueContainer()
                if let i = try? c.decode(Int.self) { self = .int(i) }
                else { self = .list(try c.decode([Int].self)) }
            }
            var first: Int {
                switch self {
                case .int(let i): return i
                case .list(let l): return l.first ?? 224
                }
            }
        }
    }

    let name: String
    let dir: URL
    let embedDim: Int
    let contextLength: Int
    let canonicalize: Bool
    let pre: PreprocessCfg
    private let padId: Int
    private let encodeText: (String) -> [Int]
    // Sessions load eagerly in init: after init the instance is immutable, so
    // concurrent requests never mutate shared state (a lazy-cache inout here
    // trips Swift's exclusivity enforcement under the concurrent server).
    // Both nil exactly when `native` is set (see init): any name in
    // SigLIPRegistry routes through native mlx-swift instead of onnxruntime.
    private let visualSession: ORTSession?
    private let textualSession: ORTSession?
    private let native: SigLIPNative?

    static let zooDir = NATIVE_CACHE_DIR.appendingPathComponent("zoo")

    // Files each model needs locally (visual + textual towers).
    static let files = [
        "config.json", "visual/model.onnx", "visual/preprocess_cfg.json",
        "textual/model.onnx", "textual/tokenizer.json", "textual/tokenizer_config.json",
    ]

    init(name: String) throws {
        // The model name arrives from the network and is used in both a cache
        // path and a download URL. Constrain it hard: the allowed charset of
        // real Immich model names, no traversal or URL metacharacters.
        guard name.range(of: "^[A-Za-z0-9._-]{1,64}$", options: .regularExpression) != nil,
              !name.hasPrefix("."), !name.contains("..")
        else {
            throw PredictError(status: "422 Unprocessable Entity",
                               message: "invalid model name")
        }
        self.name = name
        dir = Self.zooDir.appendingPathComponent(name)
        try Self.ensureFiles(name: name, dir: dir)

        // Large models keep their weights in external data files beside
        // model.onnx (#116). Resolve those once per model; the marker keeps a
        // later model switch from re-querying the API for an already-complete
        // cache. Written only after every blob is on disk, so an interrupted
        // download is retried next time.
        //
        // The marker is written only when the listing actually succeeded. A
        // transient network failure returns nil, not an empty list: recording
        // "checked" then would strand the model for good, since every later
        // load would skip the fetch and fail on the missing weights with no way
        // back short of deleting a hidden file.
        let marker = dir.appendingPathComponent(".external-data-checked")
        // Native mlx-swift path uses its own safetensors checkpoint (fetched
        // straight from the model's HF owner, see SigLIPNative), not this
        // model's ONNX weights — skip fetching several GB of external data
        // that would go unused.
        if SigLIPRegistry.config(for: name) == nil, !FileManager.default.fileExists(atPath: marker.path) {
            if let external = Self.externalDataFiles(name: name) {
                if !external.isEmpty {
                    print("[native-ml] \(name): fetching \(external.count) external data files")
                    try Self.ensureFiles(name: name, dir: dir, extra: external)
                }
                try? Data().write(to: marker)
            }
        }

        let cfg = try JSONSerialization.jsonObject(
            with: Data(contentsOf: dir.appendingPathComponent("config.json"))) as? [String: Any] ?? [:]
        // embed_dim bounds the output buffer; a wrong guess silently truncates
        // embeddings, so require the model to declare it.
        guard let dim = cfg["embed_dim"] as? Int, dim > 0, dim <= 8192 else {
            throw PredictError(status: "422 Unprocessable Entity",
                               message: "model \(name): config.json missing embed_dim")
        }
        embedDim = dim
        let textCfg = cfg["text_cfg"] as? [String: Any] ?? [:]
        contextLength = textCfg["context_length"] as? Int ?? 77
        canonicalize = ((textCfg["tokenizer_kwargs"] as? [String: Any])?["clean"] as? String) == "canonicalize"

        pre = try JSONDecoder().decode(
            PreprocessCfg.self,
            from: Data(contentsOf: dir.appendingPathComponent("visual/preprocess_cfg.json")))

        let tokCfg = try JSONSerialization.jsonObject(
            with: Data(contentsOf: dir.appendingPathComponent("textual/tokenizer_config.json"))) as? [String: Any] ?? [:]
        guard let padToken = tokCfg["pad_token"] as? String ?? (tokCfg["pad_token"] as? [String: Any])?["content"] as? String else {
            throw PredictError(status: "422 Unprocessable Entity", message: "model \(name): no pad_token")
        }

        // swift-transformers reads tokenizer.json (Unigram/sentencepiece for the
        // SigLIP family, and most others). It does not implement CLIPTokenizer,
        // so CLIP-family models fall back to our byte-level BPE (proven byte-
        // exact against the reference), reading the model's own vocab + merges.
        var loaded: Tokenizer?
        let sem = DispatchSemaphore(value: 0)
        var loadError: Error?
        let d = dir.appendingPathComponent("textual")
        Task.detached {
            do { loaded = try await AutoTokenizer.from(modelFolder: d) } catch { loadError = error }
            sem.signal()
        }
        sem.wait()
        if let tok = loaded {
            guard let pid = tok.convertTokenToId(padToken) else {
                throw PredictError(status: "422 Unprocessable Entity",
                                   message: "model \(name): pad token '\(padToken)' not in vocab")
            }
            padId = pid
            encodeText = { tok.encode(text: $0) }
        } else if (tokCfg["tokenizer_class"] as? String) == "CLIPTokenizer" {
            try Self.ensureFiles(name: name, dir: dir,
                                 extra: ["textual/vocab.json", "textual/merges.txt"])
            let bpe = CLIPTokenizer(modelDir: d.path)
            guard let pid = bpe.tokenId(padToken) else {
                throw PredictError(status: "422 Unprocessable Entity",
                                   message: "model \(name): pad token '\(padToken)' not in BPE vocab")
            }
            padId = pid
            encodeText = { bpe.encode($0) }
        } else {
            throw PredictError(status: "422 Unprocessable Entity",
                               message: "model \(name): tokenizer unsupported (\(String(describing: loadError)))")
        }

        if let sig = SigLIPRegistry.config(for: name) {
            native = try SigLIPNative(config: sig, weightsPath: SigLIPNative.ensureWeights(hfRepo: sig.hfRepo, name: name))
            visualSession = nil
            textualSession = nil
        } else {
            native = nil
            visualSession = try Self.loadSession(dir: dir, name: name, tower: "visual", dim: embedDim)
            textualSession = try Self.loadSession(dir: dir, name: name, tower: "textual", dim: embedDim)
        }
    }

    private static func loadSession(dir: URL, name: String, tower: String, dim: Int) throws -> ORTSession {
        let path = dir.appendingPathComponent("\(tower)/model.onnx").path
        guard let s = ORTSession(modelPath: path, outDim: dim) else {
            throw PredictError(status: "422 Unprocessable Entity",
                               message: "model \(name): cannot load \(tower) model")
        }
        return s
    }

    // MARK: - inference

    func embedVisual(_ cg: CGImage) throws -> [Float] {
        if let native {
            return native.embedVisual(cg, targetSize: pre.size.first, mean: pre.mean, std: pre.std)
        }
        guard let session = visualSession else {
            throw PredictError(status: "500 Internal Server Error", message: "no visual backend (\(name))")
        }
        let size = pre.size.first
        let (rgb, w, h) = rgbBuffer(cg)
        // Exact immich_ml resize_pil: short side -> size, long side int() truncated.
        let newW: Int, newH: Int
        if w < h {
            newW = size
            newH = Int(Double(h) / Double(w) * Double(size))
        } else {
            newW = Int(Double(w) / Double(h) * Double(size))
            newH = size
        }
        let resized = (newW == w && newH == h) ? rgb : Resize.bicubic(rgb, w: w, h: h, outW: newW, outH: newH)
        // Exact immich_ml crop_pil: int() centers.
        let left = Int(Double(newW) / 2 - Double(size) / 2)
        let upper = Int(Double(newH) / 2 - Double(size) / 2)
        var x = [Float](repeating: 0, count: 3 * size * size)
        for c in 0..<3 {
            for yy in 0..<size {
                for xx in 0..<size {
                    let px = Float(resized[((upper + yy) * newW + (left + xx)) * 3 + c]) / 255.0
                    x[c * size * size + yy * size + xx] = (px - pre.mean[c]) / pre.std[c]
                }
            }
        }
        guard let e = session.runMulti(
            [.float(x, shape: [1, 3, Int64(size), Int64(size)])], outDim: embedDim)
        else { throw PredictError(status: "500 Internal Server Error", message: "visual inference failed (\(name))") }
        return e
    }

    func embedTextual(_ text: String) throws -> [Float] {
        // Exact immich_ml clean_text (+ canonicalize for SigLIP-family).
        var t = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        if canonicalize {
            t = t.filter { !$0.isPunctuation }.lowercased()
        }
        var ids = encodeText(t)
        let realCount = min(ids.count, contextLength)
        if ids.count > contextLength {
            // Keep the trailing EOT/EOS: CLIP towers pool at the EOT position
            // (argmax baked into the exported graph) and SigLIP pools the last
            // token. A plain prefix drops it and yields a garbage embedding.
            let eot = ids.last!
            ids = Array(ids.prefix(contextLength - 1)) + [eot]
        }
        while ids.count < contextLength { ids.append(padId) }

        if let native { return native.embedTextual(ids) }
        guard let session = textualSession else {
            throw PredictError(status: "500 Internal Server Error", message: "no textual backend (\(name))")
        }

        func intTensor(_ v: [Int], type: Int) -> ORTSession.Tensor {
            type == 2 ? .int64(v.map(Int64.init), shape: [1, Int64(contextLength)])
                      : .int32(v.map(Int32.init), shape: [1, Int64(contextLength)])
        }
        var inputs: [ORTSession.Tensor] = [intTensor(ids, type: session.inputElemType(0))]
        if session.inputCount() == 2 {
            // Multilingual towers (XLM-Roberta / nllb families) take an
            // attention mask as the second input: 1 for real tokens, 0 for pad.
            let mask = (0..<contextLength).map { $0 < realCount ? 1 : 0 }
            inputs.append(intTensor(mask, type: session.inputElemType(1)))
        }
        guard let e = session.runMulti(inputs, outDim: embedDim) else {
            throw PredictError(status: "500 Internal Server Error", message: "textual inference failed (\(name))")
        }
        return e
    }

    // Download missing model files from Immich's HF repos (same source the
    // Python service uses). Blocking; runs on the request thread like immich_ml.
    // Dedicated session: 60s between-bytes timeout, 1h whole-file ceiling so a
    // stalled transfer fails instead of hanging a request thread indefinitely.
    private static let downloadSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 60
        cfg.timeoutIntervalForResource = 3600
        return URLSession(configuration: cfg)
    }()

    // Weights for a large model don't fit in the .onnx protobuf, so the export
    // stores them as ONNX *external data*: sibling files next to model.onnx that
    // onnxruntime opens by relative name at load time (#116). Fetching only the
    // fixed list above would leave a graph with no weights, which fails to load.
    //
    // The blobs are the direct children of a tower directory that aren't the
    // graph, a config/tokenizer file, or a build for another runtime. Identify
    // them by exclusion, because their names are otherwise arbitrary: some look
    // extension-less (onnx__MatMul_5088) and others are dotted
    // (text.transformer.resblocks.0.attn.in_proj_bias), so no single naming rule
    // covers them. Skipping the other-runtime builds matters: model.armnn is
    // 465 MB on ViT-B-32 and the rknpu/ subdirs are several GB, none of it used
    // here. Self-contained models return nothing and download exactly as before.
    static let nonWeightSuffixes = [".onnx", ".armnn", ".rknn", ".json", ".txt", ".md"]

    // Returns nil when the file list could not be retrieved (so the caller can
    // retry later), and an empty array when the model genuinely has none.
    static func externalDataFiles(name: String) -> [String]? {
        let api = URL(string: "https://huggingface.co/api/models/immich-app/\(name)")!
        var req = URLRequest(url: api)
        req.timeoutInterval = 30
        let sem = DispatchSemaphore(value: 0)
        var payload: Data?
        downloadSession.dataTask(with: req) { d, _, _ in payload = d; sem.signal() }.resume()
        sem.wait()
        guard let payload,
              let obj = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
              let siblings = obj["siblings"] as? [[String: Any]]
        else {
            // Offline or API change. Report "unknown" rather than "none" so the
            // caller retries on the next load instead of caching the failure.
            print("[native-ml] warning: could not list files for \(name)")
            return nil
        }
        return siblings.compactMap { $0["rfilename"] as? String }.filter { f in
            guard let tower = f.split(separator: "/").first,
                  tower == "visual" || tower == "textual" else { return false }
            let rest = f.dropFirst(tower.count + 1)
            guard !rest.contains("/") else { return false }  // rknpu/ and friends
            let lower = rest.lowercased()
            return !Self.nonWeightSuffixes.contains { lower.hasSuffix($0) }
        }
    }

    // Publishes what a long model fetch is doing so the menu bar can say
    // "downloading" instead of leaving the user staring at failing jobs. The
    // largest models are several GB, and the fetch runs on the request thread
    // (as immich_ml does), so Immich's ML calls time out and retry until it
    // completes. Retries are harmless: finished files are skipped.
    struct FetchProgress: Sendable {
        var model: String
        var done: Int
        var total: Int
    }

    private static let progressLock = NSLock()
    nonisolated(unsafe) private static var _progress: FetchProgress?

    static var fetchProgress: FetchProgress? {
        progressLock.lock(); defer { progressLock.unlock() }
        return _progress
    }

    private static func setProgress(_ p: FetchProgress?) {
        progressLock.lock(); _progress = p; progressLock.unlock()
    }

    static func ensureFiles(name: String, dir: URL, extra: [String] = []) throws {
        let all = files + extra
        // Only advertise a fetch worth reporting: the fixed handful is quick,
        // the external-data pass is the one measured in gigabytes.
        let reportable = all.count > files.count
        var completed = 0
        if reportable { setProgress(FetchProgress(model: name, done: 0, total: all.count)) }
        defer { if reportable { setProgress(nil) } }

        for f in all {
            let dst = dir.appendingPathComponent(f)
            if (try? dst.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0) ?? 0 > 0 {
                completed += 1
                if reportable {
                    setProgress(FetchProgress(model: name, done: completed, total: all.count))
                }
                continue
            }
            try FileManager.default.createDirectory(
                at: dst.deletingLastPathComponent(), withIntermediateDirectories: true)
            let url = URL(string: "https://huggingface.co/immich-app/\(name)/resolve/main/\(f)")!

            // A large model is hundreds of files and several GB, so a single
            // dropped connection should not fail the whole fetch and leave the
            // model unusable. Retry a few times with backoff; anything already
            // on disk is skipped, so a retry only re-fetches what is missing.
            var stored = false
            var lastError = ""
            for attempt in 1...3 {
                let sem = DispatchSemaphore(value: 0)
                var result: URL?
                var status = 0
                var moveError: Error?
                var transportError: Error?
                let task = downloadSession.downloadTask(with: url) { tmp, resp, err in
                    result = tmp.flatMap { t -> URL? in
                        // move out of the ephemeral location before the handler returns
                        let hold = FileManager.default.temporaryDirectory
                            .appendingPathComponent(UUID().uuidString)
                        do { try FileManager.default.moveItem(at: t, to: hold); return hold }
                        catch { moveError = error; return nil }
                    }
                    status = (resp as? HTTPURLResponse)?.statusCode ?? 0
                    transportError = err
                    sem.signal()
                }
                task.resume()
                sem.wait()

                guard status == 200, let tmp = result else {
                    lastError = "HTTP \(status)"
                        + (transportError.map { ", \($0.localizedDescription)" } ?? "")
                        + (moveError.map { ", \($0)" } ?? "")
                    // A 4xx is the server telling us this file will never
                    // arrive; only retry what could plausibly succeed later.
                    if (400...499).contains(status) { break }
                    if attempt < 3 {
                        print("[native-ml] retrying \(name)/\(f) (\(lastError))")
                        Thread.sleep(forTimeInterval: Double(attempt) * 2)
                    }
                    continue
                }
                do {
                    // replaceItemAt requires an atomic rename and throws
                    // EXDEV ("Cross-device link") instead of falling back
                    // when dst's volume differs from tmp's (any cache
                    // directory on an external or secondary disk, not just
                    // a theoretical case — verified on-device); moveItem
                    // handles that cross-volume case correctly.
                    try? FileManager.default.removeItem(at: dst)
                    try FileManager.default.moveItem(at: tmp, to: dst)
                } catch {
                    throw PredictError(status: "500 Internal Server Error",
                                       message: "model \(name): cannot store \(f) (\(error))")
                }
                stored = true
                break
            }
            guard stored else {
                throw PredictError(status: "422 Unprocessable Entity",
                                   message: "model \(name): download failed for \(f) (\(lastError))")
            }

            completed += 1
            if reportable {
                setProgress(FetchProgress(model: name, done: completed, total: all.count))
                // One line per file is unreadable at 500 files; log milestones.
                if completed % 25 == 0 || completed == all.count {
                    print("[native-ml] \(name): \(completed)/\(all.count) files")
                }
            } else {
                print("[native-ml] fetched \(name)/\(f)")
            }
        }
    }
}
