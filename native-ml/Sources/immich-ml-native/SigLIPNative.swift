import CoreGraphics
import Foundation
import MLX
import MLXNN

// Native MLX SigLIP / SigLIP2 — generic dual-encoder (vision + text) engine
// covering every model in this family Immich's model zoo offers, not just
// SO400M. Architecture ported from Blaizzy/mlx-embeddings
// (mlx_embeddings/models/siglip.py), which is itself config-driven across
// every SigLIP/SigLIP2 scale — this file mirrors that: one set of forward
// passes, per-model dims supplied by SigLIPRegistry.
//
// Weights are fetched directly from the checkpoint's owner on
// huggingface.co/google/<repo> (the canonical upstream release every one of
// these Immich model names is itself built from) rather than a third-party
// mlx-community conversion: that org only has a handful of SigLIP conversions
// (no coverage for most scales), so depending on it per-model is a dead end
// once more than one or two variants are in play. The raw checkpoints are:
//   - single-level key nesting (vision_model.X / text_model.X), simpler than
//     mlx-community's conversion which adds an extra vision_model.vision_model
//     wrapper layer (an artifact of mlx-embeddings' own module hierarchy).
//   - native fp32 upstream (verified via safetensors header on
//     so400m-patch16-384: 4.54GB vs. mlx-community's 2.3GB fp16 conversion of
//     the same weights) — loaded here and downcast once to bfloat16
//     (computeDType below). Apple GPU fp32 matmul throughput is roughly half
//     of bf16, and bf16 keeps fp32's full exponent range (unlike fp16), so it
//     doesn't reintroduce the fp16-text-tower upcast gotcha the original
//     SO400M-only port needed. Immich's own accuracy reference ("testing was
//     done at f32 precision") is about matching ONNX's *output*, which is
//     what matters for nearest-neighbor search — not about every intermediate
//     matmul needing to run at fp32. See ln()/embedVisual/embedTextual for
//     where reductions are deliberately upcast back to fp32 despite that.
//     Validated across all 17 registry models on a realistic 4032x3024 photo
//     (scripts/native-ml-full-benchmark.py's real test image): worst-case
//     cosine similarity against this same file's previous all-fp32 output is
//     0.9996 visual / 0.9997 textual — well inside the ~0.99 already accepted
//     for ViT-L-16-SigLIP2-256's fp32-vs-ONNX drift below. Latency: 7-21%
//     faster on top of the CPU-preprocessing win (bigger models and the
//     preprocessing-free textual path see the larger share, as expected from
//     a change that's purely about matmul throughput/bandwidth).
//   - PyTorch OIHW conv layout ([out,in,kH,kW]), not mlx-community's
//     pre-transposed HWIO — init() transposes patch_embedding.weight once,
//     matching mlx_embeddings' sanitize().
final class SigLIPNative {
    static let EPS: Float = 1e-6
    // bf16 (not fp16): same exponent range as fp32, so no overflow/underflow
    // risk in exchange for the ~2x matmul throughput / bandwidth win on
    // Apple GPU. Mantissa precision loss is real but the family already
    // tolerates it — see the ViT-L-16-SigLIP2-256 registry comment below on
    // ~0.99 cosine drift from ordinary fp32 non-associativity alone.
    static let computeDType: DType = .bfloat16

    let cfg: SigLIPConfig
    let W: [String: MLXArray]

    init(config: SigLIPConfig, weightsPath: String) throws {
        cfg = config
        var raw = try MLX.loadArrays(url: URL(fileURLWithPath: weightsPath))
        raw = raw.mapValues { $0.dtype == Self.computeDType ? $0 : $0.asType(Self.computeDType) }
        // PyTorch OIHW [out,in,kH,kW] -> MLX-friendly HWIO [out,kH,kW,in].
        // patches() below flattens each patch in (row, col, channel) order,
        // which after this transpose is patch_embedding.weight's own
        // contiguous order, so reshape([hidden, patch*patch*3]) needs no
        // further shuffle.
        let peKey = "vision_model.embeddings.patch_embedding.weight"
        if let pe = raw[peKey] { raw[peKey] = pe.transposed(0, 2, 3, 1) }
        W = raw
    }

    private func w(_ k: String) -> MLXArray {
        guard let v = W[k] else { fatalError("SigLIPNative(\(cfg.hfRepo)): missing weight \(k)") }
        return v
    }

    // Reduction (mean/variance over `hidden`, up to 1536 elements) upcast to
    // fp32 regardless of x's dtype: layernorm stats are exactly the kind of
    // wide low-magnitude-variance reduction bf16's 8-bit mantissa handles
    // poorly, and this costs nothing next to the O(hidden^2) matmuls around
    // it. Standard mixed-precision practice (e.g. PyTorch autocast keeps
    // LayerNorm in fp32 under bf16/fp16 autocast too).
    private func ln(_ x: MLXArray, _ p: String) -> MLXArray {
        let x32 = x.dtype == .float32 ? x : x.asType(.float32)
        let normed = (x32 - mean(x32, axis: -1, keepDims: true))
            * rsqrt(variance(x32, axis: -1, keepDims: true) + Self.EPS)
        return normed.asType(x.dtype) * w("\(p).weight") + w("\(p).bias")
    }
    private func lin(_ x: MLXArray, _ p: String) -> MLXArray {
        matmul(x, w("\(p).weight").transposed()) + w("\(p).bias")
    }

    // Non-causal, unmasked self-attention (both towers: SigLIP2 has no causal
    // mask, and text has no padding mask either — see embedTextual). Uses
    // MLX's fused attention kernel, which upcasts softmax to float32
    // internally regardless of input dtype — see SigLIPConfig.Tower doc for
    // why that mattered on SO400M's original fp16 checkpoint; harmless now
    // that every checkpoint here loads as fp32, kept for the same fused-
    // kernel performance win.
    private func selfAttn(_ x: MLXArray, _ p: String, _ tower: SigLIPConfig.Tower) -> MLXArray {
        let seq = x.dim(0)
        func heads(_ t: MLXArray) -> MLXArray {
            t.reshaped([1, seq, tower.heads, tower.headDim]).transposed(0, 2, 1, 3)
        }
        let q = heads(lin(x, "\(p).q_proj"))
        let k = heads(lin(x, "\(p).k_proj"))
        let v = heads(lin(x, "\(p).v_proj"))
        let o = scaledDotProductAttention(queries: q, keys: k, values: v, scale: tower.scale, mask: nil)
            .transposed(0, 2, 1, 3).reshaped([seq, tower.hidden])
        return lin(o, "\(p).out_proj")
    }

    // Shared pre-LN transformer block: vision and text encoder layers are
    // architecturally identical modulo width/depth/head-count, which is why
    // one method serves both towers given their own Tower config.
    private func block(_ x: MLXArray, _ p: String, _ tower: SigLIPConfig.Tower) -> MLXArray {
        var r = x
        let h = r + selfAttn(ln(r, "\(p).layer_norm1"), "\(p).self_attn", tower)
        r = h
        var m = ln(h, "\(p).layer_norm2")
        m = lin(m, "\(p).mlp.fc1")
        m = geluApproximate(m)      // gelu_pytorch_tanh, matches act_kwargs.approximate="tanh"
        m = lin(m, "\(p).mlp.fc2")
        return r + m
    }

    // Multihead attention-pooling head (MAP): a single learned probe
    // cross-attends over the post-layernorm patch tokens. Fused QKV
    // in_proj: rows [0..<hidden) project the probe (Q), rows [hidden..<3*hidden)
    // project the patch tokens into concatenated K;V (PyTorch
    // nn.MultiheadAttention convention, carried through mlx-embeddings'
    // sanitize()). Present on every SigLIP/SigLIP2 scale Immich selects
    // (vision_use_head defaults true in the HF config for all of them).
    private func mapHead(_ x: MLXArray) -> MLXArray {
        let tower = cfg.vision
        let probe = w("vision_model.head.probe").reshaped([1, tower.hidden])
        // PyTorch's native nn.MultiheadAttention keeps these as direct,
        // underscore-named parameters (not a nested submodule like out_proj,
        // which is dotted) — verified against the raw checkpoint's own key names.
        let inW = w("vision_model.head.attention.in_proj_weight")
        let inB = w("vision_model.head.attention.in_proj_bias")
        let qW = inW[0 ..< tower.hidden], kvW = inW[tower.hidden ..< (3 * tower.hidden)]
        let qB = inB[0 ..< tower.hidden], kvB = inB[tower.hidden ..< (3 * tower.hidden)]

        let q = matmul(probe, qW.transposed()) + qB
        let kv = matmul(x, kvW.transposed()) + kvB
        let k = kv[.ellipsis, 0 ..< tower.hidden]
        let v = kv[.ellipsis, tower.hidden ..< (2 * tower.hidden)]

        let numPatches = x.dim(0)
        func heads(_ t: MLXArray, _ seq: Int) -> MLXArray {
            t.reshaped([1, seq, tower.heads, tower.headDim]).transposed(0, 2, 1, 3)
        }
        let qh = heads(q, 1), kh = heads(k, numPatches), vh = heads(v, numPatches)
        var o = scaledDotProductAttention(queries: qh, keys: kh, values: vh, scale: tower.scale, mask: nil)
            .transposed(0, 2, 1, 3).reshaped([1, tower.hidden])
        o = lin(o, "vision_model.head.attention.out_proj")

        let residual = o
        var hs = ln(o, "vision_model.head.layernorm")
        hs = lin(hs, "vision_model.head.mlp.fc1")
        hs = geluApproximate(hs)
        hs = lin(hs, "vision_model.head.mlp.fc2")
        return residual + hs
    }

    // resize_mode=squash (every SigLIP/SigLIP2 model's preprocess_cfg.json):
    // direct bicubic resize to targetSize x targetSize, no aspect-preserving
    // crop. mean/std/targetSize come from the caller's own preprocess_cfg.json
    // fetch (ZooCLIP.pre) rather than being hardcoded here — SigLIP's 0.5/0.5
    // normalization is a stable convention across the family, but sourcing it
    // from Immich's own per-model config (already fetched for the tokenizer
    // and ONNX path) means this file carries no per-model preprocessing data
    // beyond patch size, and can't silently drift from what Immich actually
    // exported. See the so400m-patch14-378 case: its own HF config claims
    // image_size=384, which is misleading for a patch14 grid (384/14 isn't
    // integer) — Immich's preprocess_cfg.json is the ground truth for the
    // resize target, the HF vision config is not.
    // Row-parallel (per patch-grid row) and unsafe-pointer-backed: this scalar
    // im2col loop runs on every call, sized by the *source* image's patch grid,
    // and was measured as a real chunk of latency alongside Resize.bicubic — see
    // that file's doc comment. Same output as the previous bounds-checked
    // sequential version, just faster on the M4's 10 cores.
    // Returns the raw normalized patch buffer and its shape, NOT an MLXArray.
    // Building the array here would put MLX allocation and a lazy astype node
    // outside withMetalLock, on up to Server.swift's maxConcurrent threads at
    // once, which is the invariant GPULock.swift exists to hold. Only the
    // pixel work belongs outside the lock; the caller makes the array inside.
    private func patches(_ cg: CGImage, targetSize: Int, mean: [Float], std: [Float])
        -> (flat: [Float], rows: Int, cols: Int) {
        let patch = cfg.visionPatch
        let grid = targetSize / patch
        let (full, iw, ih) = rgbBuffer(cg)
        let resized = (iw == targetSize && ih == targetSize)
            ? full : Resize.bicubic(full, w: iw, h: ih, outW: targetSize, outH: targetSize)
        let dim = 3 * patch * patch
        var flat = [Float](repeating: 0, count: grid * grid * dim)
        resized.withUnsafeBufferPointer { src in
            flat.withUnsafeMutableBufferPointer { dst in
                DispatchQueue.concurrentPerform(iterations: grid) { pi in
                    for pj in 0 ..< grid {
                        let p = pi * grid + pj
                        for i in 0 ..< patch {
                            let py = pi * patch + i
                            for j in 0 ..< patch {
                                let px = pj * patch + j
                                let srcBase = (py * targetSize + px) * 3
                                let dstBase = p * dim + i * patch * 3 + j * 3
                                for c in 0 ..< 3 {
                                    let v = Float(src[srcBase + c]) / 255.0
                                    dst[dstBase + c] = (v - mean[c]) / std[c]
                                }
                            }
                        }
                    }
                }
            }
        }
        return (flat, grid * grid, dim)
    }

    // Scoped to the GPU device for just this call (Device.withDefaultDevice
    // sets a @TaskLocal, not the process-wide default) — every model in this
    // family is dramatically slower on CPU (SO400M measured ~28x), but the
    // rest of the service (default mlx CLIP path, Vision-framework OCR/face
    // detection) must keep running on whatever the global default is.
    // Device.gpu / its .defaultStream are pre-existing cached singletons
    // (see mlx-swift's Device.swift), so this allocates nothing — earlier
    // this used Stream.withNewDefaultStream(device:), which constructs a
    // brand-new Metal command queue (mlx_stream_new_device) on every single
    // call. Under sustained sequential load that churn is a real suspect for
    // a production Metal OOM crash ("Command buffer execution failed:
    // Insufficient Memory") after hundreds of consecutive predict calls;
    // reusing the cached stream removes the churn entirely.
    // Concurrency: guarded by metalLock (GPULock.swift), the Swift-side
    // equivalent of the Python fork's gpu_lock — see that file for why.
    func embedVisual(_ cg: CGImage, targetSize: Int, mean: [Float], std: [Float]) -> [Float] {
        // CPU-only (image decode, bicubic resize, im2col) — deliberately outside
        // withMetalLock so up to Server.swift's maxConcurrent requests can prep
        // their patch tensors in parallel; only the GPU submission below needs
        // to be serialized against other Metal work (see GPULock.swift).
        let prepped = patches(cg, targetSize: targetSize, mean: mean, std: std)
        return withMetalLock {
            Device.withDefaultDevice(.gpu) {
                let patchInput = MLXArray(prepped.flat, [prepped.rows, prepped.cols])
                    .asType(Self.computeDType)
                let tower = cfg.vision
                let wPatch = w("vision_model.embeddings.patch_embedding.weight")
                    .reshaped([tower.hidden, 3 * cfg.visionPatch * cfg.visionPatch])
                var x = matmul(patchInput, wPatch.transposed())
                x = x + w("vision_model.embeddings.patch_embedding.bias")
                x = x + w("vision_model.embeddings.position_embedding.weight")
                for l in 0 ..< tower.layers {
                    x = block(x, "vision_model.encoder.layers.\(l)", tower)
                }
                x = ln(x, "vision_model.post_layernorm")
                // L2-normalize in fp32 regardless of computeDType: this is the
                // vector that actually gets compared by cosine similarity in
                // search, and the sum-of-squares reduction has the same
                // wide-reduction precision risk as ln() above — cheap to
                // upcast for a single `hidden`-length vector.
                let raw = mapHead(x).reshaped([tower.hidden])
                let emb32 = raw.dtype == .float32 ? raw : raw.asType(.float32)
                let normed = emb32 / sqrt((emb32 * emb32).sum())
                eval(normed)
                return normed.asArray(Float.self)
            }
        }
    }

    // ids: already tokenized (canonicalize + encode + EOS-preserving
    // truncate + pad to context_length), matching ZooCLIP's existing
    // embedTextual pipeline. Pools the fixed last position, unmasked — the
    // ONNX graph itself takes no attention_mask input for this family
    // (single-input textual tower), relying on non-causal self-attention to
    // let the last position see the whole real sequence regardless of
    // trailing padding ("sticky EOS" convention).
    func embedTextual(_ ids: [Int]) -> [Float] {
        withMetalLock {
            Device.withDefaultDevice(.gpu) {
                let tower = cfg.text
                let seq = ids.count
                let tokEmb = w("text_model.embeddings.token_embedding.weight")
                let posEmb = w("text_model.embeddings.position_embedding.weight")
                let idsArr = MLXArray(ids.map { Int32($0) })
                var x = take(tokEmb, idsArr, axis: 0) + posEmb[0 ..< seq]
                for l in 0 ..< tower.layers {
                    x = block(x, "text_model.encoder.layers.\(l)", tower)
                }
                x = ln(x, "text_model.final_layer_norm")
                let pooled = x[seq - 1].reshaped([1, tower.hidden])
                // fp32 L2-normalize — see the matching comment in embedVisual.
                let raw = lin(pooled, "text_model.head").reshaped([cfg.textProjection])
                let emb32 = raw.dtype == .float32 ? raw : raw.asType(.float32)
                let normed = emb32 / sqrt((emb32 * emb32).sum())
                eval(normed)
                return normed.asArray(Float.self)
            }
        }
    }

    // MARK: - weight fetch

    static let cacheDir = NATIVE_CACHE_DIR.appendingPathComponent("mlx-siglip")

    // An optional token raises HF's rate limit and (per HF's own CDN response
    // headers on an anonymous request: "unauthenticated ... set HF_TOKEN to
    // enable higher rate limits and faster downloads") can improve throughput.
    // Strictly additive: unset is the common case and behaves exactly as
    // before (anonymous request).
    private static var hfAuthHeaders: [String: String] {
        guard let token = ProcessInfo.processInfo.environment["HF_TOKEN"], !token.isEmpty else { return [:] }
        return ["Authorization": "Bearer \(token)"]
    }

    // One safetensors file per model (~1-4.5GB, fp32); a framework-agnostic
    // format mlx-swift loads directly, so no conversion step is needed
    // on-device. Cached per Immich model name so switching between e.g.
    // ViT-B-16-SigLIP2 and ViT-L-16-SigLIP2-256 doesn't re-fetch either one
    // a second time.
    static func ensureWeights(hfRepo: String, name: String) throws -> String {
        let dir = cacheDir.appendingPathComponent(name)
        let dst = dir.appendingPathComponent("model.safetensors")
        if ((try? dst.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) ?? 0 > 0 {
            return dst.path
        }
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        sweepPartials(dir: dir, name: name)
        let url = URL(string: "https://huggingface.co/\(hfRepo)/resolve/main/model.safetensors")!
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 60
        cfg.timeoutIntervalForResource = 3600
        cfg.httpAdditionalHeaders = hfAuthHeaders
        let session = URLSession(configuration: cfg)

        // HF's CDN (CloudFront-backed) accepts byte ranges; one TCP stream
        // measured ~18MB/s on-device for these files, well under what the
        // CDN can serve to several concurrent streams. Split into ranged
        // GETs fetched in parallel, written into a preallocated temp file at
        // each chunk's own offset, then atomically moved into place only
        // once every chunk has landed — dst is never a partially-written
        // file, matching the single-connection path's own all-or-nothing
        // contract (a truncated placeholder there would pass the size>0
        // check above without the weights actually being complete).
        if let total = contentLength(session: session, url: url), total > 0 {
            do {
                try downloadRanged(session: session, url: url, dst: dst, total: total, name: name)
                return dst.path
            } catch {
                print("[native-ml] \(name): parallel fetch failed (\(error)), falling back to single connection")
            }
        }
        try downloadWhole(session: session, url: url, dst: dst, name: name)
        return dst.path
    }

    // Delete leftover .tmp-<uuid> files from an interrupted fetch.
    //
    // downloadRanged preallocates the whole file (truncate to Content-Length)
    // before writing a byte, so an interrupted 4.5GB fetch leaves something
    // that reports its full size on disk and is named differently every time
    // — nothing would ever have reclaimed it, and each retry added another.
    // Only safe here, before a fetch starts, because a completed download is
    // already renamed to model.safetensors and a concurrent one cannot exist:
    // Models.zoo(for:) holds a single-resident-model lock across this call.
    private static func sweepPartials(dir: URL, name: String) {
        let fm = FileManager.default
        guard let entries = try? fm.contentsOfDirectory(atPath: dir.path) else { return }
        for entry in entries where entry.hasPrefix(".tmp-") {
            let stale = dir.appendingPathComponent(entry)
            let bytes = ((try? stale.resourceValues(forKeys: [.totalFileAllocatedSizeKey])
                .totalFileAllocatedSize) ?? 0) ?? 0
            try? fm.removeItem(at: stale)
            print("[native-ml] \(name): discarded interrupted download (\(bytes / 1024 / 1024)MB)")
        }
    }

    private static func contentLength(session: URLSession, url: URL) -> Int? {
        var req = URLRequest(url: url)
        req.httpMethod = "HEAD"
        let sem = DispatchSemaphore(value: 0)
        var total: Int?
        var acceptsRanges = false
        var ok = false
        session.dataTask(with: req) { _, resp, _ in
            if let http = resp as? HTTPURLResponse {
                // HF's origin unconditionally advertises Accept-Ranges: bytes
                // even on a 404 (verified on-device against a sharded
                // checkpoint with no single model.safetensors) — the error
                // page's own small body would otherwise be mistaken for a
                // tiny real file. Status must be checked explicitly.
                ok = http.statusCode == 200
                total = Int(http.value(forHTTPHeaderField: "Content-Length") ?? "")
                acceptsRanges = http.value(forHTTPHeaderField: "Accept-Ranges") == "bytes"
            }
            sem.signal()
        }.resume()
        sem.wait()
        return (ok && acceptsRanges) ? total : nil
    }

    private static func downloadRanged(session: URLSession, url: URL, dst: URL, total: Int, name: String) throws {
        // Same directory as dst, not FileManager's system temp dir: when the
        // cache lives on a different volume than /var/folders (an external
        // or secondary disk — not just theoretical, hit this on-device),
        // FileManager.moveItem below still succeeds (it falls back to a
        // copy across volumes, verified), but writing here directly avoids
        // that fallback doing a redundant full-file copy on top of the
        // chunk writes this function already did.
        let tmp = dst.deletingLastPathComponent().appendingPathComponent(".tmp-\(UUID().uuidString)")
        guard FileManager.default.createFile(atPath: tmp.path, contents: nil) else {
            throw PredictError(status: "500 Internal Server Error", message: "\(name): cannot create temp file")
        }
        let handle = try FileHandle(forWritingTo: tmp)
        defer { try? handle.close() }
        try handle.truncate(atOffset: UInt64(total))
        let writeLock = NSLock()

        let chunks = 8
        let chunkSize = (total + chunks - 1) / chunks
        let ranges = (0..<chunks).map { i in (i * chunkSize, min((i + 1) * chunkSize, total) - 1) }
            .filter { $0.0 <= $0.1 }
        print("[native-ml] fetching \(name) (\(total / 1024 / 1024)MB, \(ranges.count)x parallel)")
        // Multi-gigabyte and minutes long, so it goes on the same /health field
        // the ONNX fetch uses. Without this the menu bar showed nothing at all
        // during the single longest wait the service has, while Immich's jobs
        // failed and retried in the background and the app looked idle.
        ZooCLIP.setProgress(.init(model: name, done: 0, total: ranges.count))
        defer { ZooCLIP.setProgress(nil) }

        let group = DispatchGroup()
        let errorLock = NSLock()
        var firstError: Error?
        var completed = 0
        for (start, end) in ranges {
            group.enter()
            // Dispatched, not called inline. fetchRange blocks its caller until
            // the transfer finishes, so running it in this loop made "8x
            // parallel" strictly sequential: every group.leave() had already
            // run by the time group.wait() was reached, and a 4.5GB checkpoint
            // took the full single-stream time while the label claimed
            // otherwise.
            DispatchQueue.global().async {
                fetchRange(session: session, url: url, start: start, end: end) { chunkFile, err in
                    defer { group.leave() }
                    guard let chunkFile else {
                        errorLock.lock(); if firstError == nil { firstError = err }; errorLock.unlock()
                        return
                    }
                    defer { try? FileManager.default.removeItem(at: chunkFile) }
                    // A 206 is not a promise that the bytes are the ones asked
                    // for. Unchecked, a short or shifted chunk left the
                    // preallocated file's zeros in place, still counted as
                    // completed, and got published as model.safetensors — which
                    // then passes the size>0 cache check forever, so the model
                    // either fails to load on every start or loads and writes
                    // garbage embeddings into Immich's search index.
                    let expected = end - start + 1
                    let got = ((try? chunkFile.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) ?? 0
                    guard got == expected else {
                        errorLock.lock()
                        if firstError == nil {
                            firstError = PredictError(
                                status: "500 Internal Server Error",
                                message: "\(name): range \(start)-\(end) returned "
                                    + "\(got) bytes, expected \(expected)")
                        }
                        errorLock.unlock()
                        return
                    }
                    writeLock.lock()
                    defer { writeLock.unlock() }
                    do {
                        // Streamed in blocks rather than read whole. Eight
                        // concurrent chunks of a 4.5GB checkpoint held the
                        // entire file in memory at once when each chunk was an
                        // in-memory Data, on a machine also running the worker
                        // and an Immich stack.
                        let reader = try FileHandle(forReadingFrom: chunkFile)
                        defer { try? reader.close() }
                        try handle.seek(toOffset: UInt64(start))
                        while let block = try reader.read(upToCount: 8 << 20), !block.isEmpty {
                            try handle.write(contentsOf: block)
                        }
                        completed += 1
                        ZooCLIP.setProgress(.init(model: name, done: completed, total: ranges.count))
                    } catch {
                        errorLock.lock(); if firstError == nil { firstError = error }; errorLock.unlock()
                    }
                }
            }
        }
        group.wait()
        try handle.close()
        if let firstError {
            try? FileManager.default.removeItem(at: tmp)
            throw firstError
        }
        try? FileManager.default.removeItem(at: dst)
        try FileManager.default.moveItem(at: tmp, to: dst)
    }

    // One range, 3 attempts with backoff — a single dropped chunk shouldn't
    // force re-fetching the other 7/8 of a multi-GB file.
    private static func fetchRange(
        session: URLSession, url: URL, start: Int, end: Int, completion: @escaping (URL?, Error?) -> Void
    ) {
        var req = URLRequest(url: url)
        req.setValue("bytes=\(start)-\(end)", forHTTPHeaderField: "Range")
        var lastError: Error?
        for attempt in 1...3 {
            let sem = DispatchSemaphore(value: 0)
            var result: URL?
            var ignoredRange = false
            // downloadTask, not dataTask: the body lands in a file instead of
            // in memory, so eight concurrent chunks cost eight file handles
            // rather than the whole checkpoint in RAM.
            session.downloadTask(with: req) { tmp, resp, err in
                let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
                if status == 206, let tmp {
                    // URLSession deletes its temp file when this handler
                    // returns, so move it somewhere we own first.
                    let hold = FileManager.default.temporaryDirectory
                        .appendingPathComponent("chunk-\(UUID().uuidString)")
                    if (try? FileManager.default.moveItem(at: tmp, to: hold)) != nil {
                        result = hold
                    }
                } else if status == 200 {
                    // The Range header was ignored and the whole file came
                    // back. contentLength() gates on the origin's
                    // Accept-Ranges, which says nothing about what a proxy in
                    // between does. Retrying cannot help and is ruinously
                    // expensive: each of 8 chunks would pull the entire
                    // multi-gigabyte file three times before the ranged path
                    // gave up, so give up now and let the single-connection
                    // fallback do it once.
                    ignoredRange = true
                } else {
                    lastError = err
                }
                sem.signal()
            }.resume()
            sem.wait()
            if let result { completion(result, nil); return }
            if ignoredRange {
                completion(nil, PredictError(status: "500 Internal Server Error",
                                             message: "server ignored Range (200), falling back"))
                return
            }
            if attempt < 3 { Thread.sleep(forTimeInterval: Double(attempt) * 2) }
        }
        completion(nil, lastError ?? PredictError(status: "500 Internal Server Error",
                                                    message: "range \(start)-\(end) failed"))
    }

    // Single-connection fallback: used when the server doesn't advertise
    // Content-Length/Accept-Ranges, or if the ranged path fails outright.
    private static func downloadWhole(session: URLSession, url: URL, dst: URL, name: String) throws {
        // One opaque transfer, so there is no chunk count to report against;
        // 0-of-1 still tells the menu bar which model is being fetched, which
        // is the part that keeps a long wait from looking like a hang.
        ZooCLIP.setProgress(.init(model: name, done: 0, total: 1))
        defer { ZooCLIP.setProgress(nil) }
        var lastError = ""
        for attempt in 1...3 {
            let sem = DispatchSemaphore(value: 0)
            var result: URL?
            var status = 0
            var transportError: Error?
            var expected = -1
            let task = session.downloadTask(with: url) { tmp, resp, err in
                if let tmp {
                    let hold = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
                    try? FileManager.default.moveItem(at: tmp, to: hold)
                    result = hold
                }
                let http = resp as? HTTPURLResponse
                status = http?.statusCode ?? 0
                expected = Int(http?.value(forHTTPHeaderField: "Content-Length") ?? "") ?? -1
                transportError = err
                sem.signal()
            }
            print("[native-ml] fetching \(name) (single connection, one-time)")
            task.resume()
            sem.wait()

            if status == 200, let tmp = result {
                // The ranged path checks every chunk against the range it asked
                // for; this one had no check at all, and it is not a rare path
                // (it is the fallback whenever ranges fail, including a proxy
                // that strips the Range header). A connection that drops
                // mid-body still yields a 200 with a short file, which would be
                // moved into place and then satisfy the fileSize > 0 cache check
                // on every subsequent start: a permanently broken model.
                let got = ((try? tmp.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) ?? 0
                if expected > 0 && got != expected {
                    try? FileManager.default.removeItem(at: tmp)
                    lastError = "short body: got \(got) bytes, expected \(expected)"
                    if attempt < 3 {
                        print("[native-ml] retrying \(name) weight fetch (\(lastError))")
                        Thread.sleep(forTimeInterval: Double(attempt) * 2)
                    }
                    continue
                }
                try? FileManager.default.removeItem(at: dst)
                try FileManager.default.moveItem(at: tmp, to: dst)
                return
            }
            lastError = "HTTP \(status)" + (transportError.map { ", \($0.localizedDescription)" } ?? "")
            if attempt < 3 {
                print("[native-ml] retrying \(name) weight fetch (\(lastError))")
                Thread.sleep(forTimeInterval: Double(attempt) * 2)
            }
        }
        throw PredictError(status: "422 Unprocessable Entity",
                           message: "\(name) weights: download failed (\(lastError))")
    }
}

// MARK: - config

// Per-model architecture dims, keyed by Immich's model name. Not derivable
// at runtime from each checkpoint's config.json: HF's PretrainedConfig omits
// any field equal to the class default (base scale, 768/12/12/3072), so a
// generic parser would need these exact per-scale numbers hardcoded anyway
// to fill the gaps. Sourced from https://huggingface.co/<hfRepo>/config.json
// for each entry (vision_config / text_config), cross-checked against the
// safetensors header where a config.json was ambiguous.
struct SigLIPConfig {
    struct Tower {
        let hidden: Int
        let layers: Int
        let heads: Int
        let intermediate: Int
        var headDim: Int { hidden / heads }
        var scale: Float { 1 / Float(headDim).squareRoot() }
    }
    let hfRepo: String
    let vision: Tower
    let visionPatch: Int
    let text: Tower
    // text_model.head projects from text.hidden to this width. Equal to
    // text.hidden for every model currently in this registry, but not a safe
    // assumption in general: the giant-opt scale's text tower runs at
    // so400m's width (1152) internally and projects up to 1536 to match its
    // own larger vision tower's output (see the deferred-models note below)
    // — caught by cross-checking every field against each model's actual
    // config.json rather than assuming vision/text symmetry. Kept as an
    // explicit field, not inferred, so giant-opt support is a registry
    // addition later rather than another architecture change.
    let textProjection: Int
}

enum SigLIPRegistry {
    static let models: [String: SigLIPConfig] = [
        "ViT-B-16-SigLIP__webli": SigLIPConfig(
            hfRepo: "google/siglip-base-patch16-224",
            vision: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072),
            visionPatch: 16,
            text: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072), textProjection: 768),
        "ViT-B-16-SigLIP2__webli": SigLIPConfig(
            hfRepo: "google/siglip2-base-patch16-224",
            vision: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072),
            visionPatch: 16,
            text: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072), textProjection: 768),
        // RE-MEASURED 2026-08-10, after the CPU-preprocessing rewrite and the
        // move to bf16 compute: this model no longer stands out. Against
        // Immich's own ONNX export on three real library preview images it
        // measures 0.99932, 0.99953 and 0.99979 visual, 0.99983 textual — in
        // line with every other model here. Whatever produced the number below
        // did not survive those two commits, so the note is kept for the
        // investigation rather than as a live caveat. Re-check on real
        // previews, not synthetic images, if it ever resurfaces: a noise image
        // is not a proxy for a photo here.
        //
        // Visual embeddings here measure ~0.99 cosine against Immich's ONNX
        // export (vs. 0.999+ for every other model in this registry); textual
        // is unaffected (1.0000). Investigated 2026-08-09 and not treated as
        // a loose end: every weight key/shape was cross-checked directly
        // against this checkpoint's own safetensors header, and an
        // independent Python reference (Blaizzy/mlx-embeddings, loaded from
        // the same raw checkpoint) matches Immich's ONNX export almost
        // exactly (0.999967) on the same input — so the config, weights, and
        // ONNX export are all fine. Layer-by-layer bisection against that
        // same Python reference shows the two implementations' per-patch
        // outputs drift apart gradually from roughly layer 10 of 24 onward,
        // unevenly: most patches stay well-aligned through the last layer
        // while a minority diverge much further. That's the signature of
        // ordinary floating-point non-associativity between two
        // independently-written, mathematically-equivalent implementations
        // compounding through repeated self-attention, not a discrete logic
        // error — so400m's own pre-pooling patches disagree even more
        // severely in isolation (one bottoms out at 0.13 cosine) yet its MAP
        // head's learned weights don't lean on that patch, so its final
        // embedding stays excellent; this model's MAP head apparently does
        // weight its noisiest patches more. Confirmed consistent (not a
        // one-image fluke) across 3 different test images (0.990-0.997).
        // Practical impact on nearest-neighbor search is believed low — two
        // encodings of the same image at 0.99 cosine still land far closer
        // to each other than to any different image — but this is flagged
        // honestly rather than asserted fine. Re-check if Phase 1's batch
        // harness finds the same pattern on other models.
        "ViT-L-16-SigLIP2-256__webli": SigLIPConfig(
            hfRepo: "google/siglip2-large-patch16-256",
            vision: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096),
            visionPatch: 16,
            text: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096), textProjection: 1024),
        "ViT-SO400M-16-SigLIP2-384__webli": SigLIPConfig(
            hfRepo: "google/siglip2-so400m-patch16-384",
            vision: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304),
            visionPatch: 16,
            text: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304), textProjection: 1152),

        // --- added for full-catalog coverage (all verified against each
        // model's own https://huggingface.co/<hfRepo>/config.json) ---

        // patch14 at 384 does not divide evenly (384/14 = 27.43): the conv
        // is a "valid" 27x27 patch grid with ~6px trimmed off the bottom-
        // right, which is why Immich names this variant "-378" (27*14) even
        // though the checkpoint's own HF config says image_size=384. The
        // resize target actually used comes from this model's own
        // preprocess_cfg.json (ZooCLIP.pre.size, already fetched generically)
        // rather than the HF vision_config here — they disagree, and
        // preprocess_cfg.json is the one Immich's own ONNX export was built
        // against.
        "ViT-SO400M-14-SigLIP2-378__webli": SigLIPConfig(
            hfRepo: "google/siglip2-so400m-patch14-384",
            vision: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304),
            visionPatch: 14,
            text: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304), textProjection: 1152),
        "ViT-SO400M-16-SigLIP2-512__webli": SigLIPConfig(
            hfRepo: "google/siglip2-so400m-patch16-512",
            vision: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304),
            visionPatch: 16,
            text: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304), textProjection: 1152),
        "ViT-L-16-SigLIP2-512__webli": SigLIPConfig(
            hfRepo: "google/siglip2-large-patch16-512",
            vision: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096),
            visionPatch: 16,
            text: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096), textProjection: 1024),
        "ViT-SO400M-16-SigLIP2-256__webli": SigLIPConfig(
            hfRepo: "google/siglip2-so400m-patch16-256",
            vision: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304),
            visionPatch: 16,
            text: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304), textProjection: 1152),
        // No resolution suffix in Immich's name = the checkpoint's default
        // (224 for patch14, matching this repo's name).
        "ViT-SO400M-14-SigLIP2__webli": SigLIPConfig(
            hfRepo: "google/siglip2-so400m-patch14-224",
            vision: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304),
            visionPatch: 14,
            text: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304), textProjection: 1152),
        "ViT-L-16-SigLIP2-384__webli": SigLIPConfig(
            hfRepo: "google/siglip2-large-patch16-384",
            vision: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096),
            visionPatch: 16,
            text: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096), textProjection: 1024),
        "ViT-B-32-SigLIP2-256__webli": SigLIPConfig(
            hfRepo: "google/siglip2-base-patch32-256",
            vision: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072),
            visionPatch: 32,
            text: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072), textProjection: 768),

        // SigLIP v1 (no "2" — smaller 32k sentencepiece vocab instead of
        // SigLIP2's 256k, handled transparently by ZooCLIP's existing
        // generic tokenizer loading; nothing here depends on vocab size).
        "ViT-SO400M-14-SigLIP-384__webli": SigLIPConfig(
            hfRepo: "google/siglip-so400m-patch14-384",
            vision: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304),
            visionPatch: 14,
            text: .init(hidden: 1152, layers: 27, heads: 16, intermediate: 4304), textProjection: 1152),
        "ViT-L-16-SigLIP-384__webli": SigLIPConfig(
            hfRepo: "google/siglip-large-patch16-384",
            vision: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096),
            visionPatch: 16,
            text: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096), textProjection: 1024),
        "ViT-L-16-SigLIP-256__webli": SigLIPConfig(
            hfRepo: "google/siglip-large-patch16-256",
            vision: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096),
            visionPatch: 16,
            text: .init(hidden: 1024, layers: 24, heads: 16, intermediate: 4096), textProjection: 1024),
        "ViT-B-16-SigLIP-512__webli": SigLIPConfig(
            hfRepo: "google/siglip-base-patch16-512",
            vision: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072),
            visionPatch: 16,
            text: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072), textProjection: 768),
        "ViT-B-16-SigLIP-384__webli": SigLIPConfig(
            hfRepo: "google/siglip-base-patch16-384",
            vision: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072),
            visionPatch: 16,
            text: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072), textProjection: 768),
        "ViT-B-16-SigLIP-256__webli": SigLIPConfig(
            hfRepo: "google/siglip-base-patch16-256",
            vision: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072),
            visionPatch: 16,
            text: .init(hidden: 768, layers: 12, heads: 12, intermediate: 3072), textProjection: 768),

        // Deliberately NOT here (deferred, not overlooked):
        //  - ViT-B-16-SigLIP-i18n-256__webli: Immich's name implies base
        //    scale, but the only "i18n" checkpoint google publishes is
        //    SO400M-scale (google/siglip-so400m-patch16-256-i18n) — the
        //    name doesn't map mechanically like the rest of this table, and
        //    guessing which repo (if any) actually backs this Immich model
        //    risks silently loading the wrong weights. Needs the real repo
        //    tracked down before it can be added.
        //  - nllb-clip-{base,large}-siglip__{v1,mrl}: SigLIP vision tower
        //    (already covered by the configs above once the right repo is
        //    confirmed) but paired with an NLLB text tower, a different
        //    architecture family this file doesn't implement. Vision-only
        //    reuse is a separate, smaller follow-up; full support needs an
        //    NLLB port, out of scope here.
        //  - ViT-gopt-16-SigLIP2-{256,384}__webli: google/siglip2-giant-opt-*
        //    ships as two sharded files (model-0000{1,2}-of-00002.safetensors
        //    + an index) rather than the single model.safetensors every
        //    other entry here has — ensureWeights doesn't handle that yet.
        //    Also the single biggest model in Immich's whole catalog (~7GB
        //    fp32 per resolution) for a scale that Immich's own docs already
        //    mark not Pareto-optimal against so400m-384 on English recall
        //    (so400m-384 scores *higher* recall at roughly a third the
        //    memory) — lowest value-per-effort entry in the catalog, so
        //    deferred rather than built now. Multi-shard loading (concatenate
        //    each shard's MLX.loadArrays() result) is the actual gap if this
        //    gets picked up later.
    ]

    static func config(for name: String) -> SigLIPConfig? { models[name] }
}
