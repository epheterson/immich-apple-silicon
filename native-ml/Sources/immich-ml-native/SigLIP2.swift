import CoreGraphics
import Foundation
import MLX
import MLXNN

// Native MLX SigLIP2 SO400M/16 384px — the one zoo model the roadmap singles
// out for its ~2500ms ONNX-CPU visual-tower cost (a CoreML EP attempt, see
// the native-ml-coreml-ep branch, made this specific model worse: MLProgram
// compile alone ran multiple minutes on its graph). Architecture ported from
// Blaizzy/mlx-embeddings (mlx_embeddings/models/siglip.py); weights are the
// mlx-community/siglip2-so400m-patch16-384 safetensors conversion, verified
// equivalent to Immich's own ONNX export (google/siglip2-so400m-patch16-384
// is the same released checkpoint upstream of both).
//
// Measured on-device against Immich's ONNX export, same test image/phrases:
// visual cosine 0.9994, textual cosine ~1.0 (5 phrases, 0.999999-1.0000001).
// Both towers are L2-normalized before return, matching what the ONNX graph
// itself bakes in (its raw output is already unit-norm). Two gotchas found
// getting here, both load-bearing for anyone touching this file:
//   1. Immich's preprocess_cfg.json for this model says resize_mode=squash
//      (direct resize, no crop) and bicubic interpolation. HF's own default
//      SiglipImageProcessor resamples bilinear instead — using it silently
//      caps visual cosine at ~0.99, not a bug in the weights or the port.
//      The remaining 0.9994 (not higher) is Resize.bicubic's own ~16/255
//      max pixel deviation from true PIL bicubic on this squash-resized
//      case — pre-existing, shared with the default CLIP path and the ONNX
//      zoo path, not specific to this file.
//   2. The checkpoint is natively fp16. That's fine for the vision tower
//      (576 genuinely distinct patches) but the text tower's fixed
//      context_length=64 means a short caption pads ~58 positions with
//      identical pad-token rows differing only by position embedding —
//      numerically ill-conditioned enough that fp16 rounding compounds
//      across 27 layers into a real divergence (0.98 cosine, reproducible,
//      not noise). init() upcasts every weight to fp32 to fix it.
final class SigLIP2SO400M {
    static let modelName = "ViT-SO400M-16-SigLIP2-384__webli"
    static let hfRepo = "mlx-community/siglip2-so400m-patch16-384"

    static let H = 1152, LAYERS = 27, HEADS = 16
    static let HEAD_DIM = H / HEADS
    static let SCALE = Float(1.0) / Float(HEAD_DIM).squareRoot()
    static let EPS: Float = 1e-6
    static let PATCH = 16, IMG = 384, GRID = IMG / PATCH   // 24
    static let NUM_PATCHES = GRID * GRID                   // 576

    let W: [String: MLXArray]

    init(weightsPath: String) throws {
        let raw = try MLX.loadArrays(url: URL(fileURLWithPath: weightsPath))
        // Upcast from the checkpoint's native fp16: the text tower's input is
        // unusually ill-conditioned for this model (context_length 64, and a
        // short real caption pads ~58 of those with identical pad-token rows
        // differing only by position embedding) — fp16 rounding differences
        // that are invisible on the 576-diverse-patch vision tower compound
        // across 27 attention layers into a measurable output divergence
        // here (0.98 cosine vs the ONNX reference, fp16; fp32 below fixes it).
        W = raw.mapValues { $0.dtype == .float16 ? $0.asType(.float32) : $0 }
    }
    private func w(_ k: String) -> MLXArray {
        guard let v = W[k] else { fatalError("SigLIP2SO400M: missing weight \(k)") }
        return v
    }

    private func ln(_ x: MLXArray, _ p: String) -> MLXArray {
        (x - mean(x, axis: -1, keepDims: true)) * rsqrt(variance(x, axis: -1, keepDims: true) + Self.EPS)
            * w("\(p).weight") + w("\(p).bias")
    }
    private func lin(_ x: MLXArray, _ p: String) -> MLXArray {
        matmul(x, w("\(p).weight").transposed()) + w("\(p).bias")
    }

    // Non-causal, unmasked self-attention (both towers: SigLIP2 has no
    // causal mask, and text has no padding mask either — see embedTextual).
    // Uses MLX's fused attention kernel rather than a manual
    // softmax(QK^T)V decomposition: it upcasts the softmax to float32
    // internally regardless of input dtype (this checkpoint is fp16), which
    // matters a lot for the text tower — 58 of 64 positions there are
    // identical pad-token rows differing only by position embedding, and a
    // plain fp16 softmax over those near-tied logits measurably diverged
    // from the Python/ONNX reference (0.98 cosine) by accumulating
    // per-layer error over 27 layers; the vision tower (576 genuinely
    // distinct patches) wasn't nearly as sensitive to this.
    private func selfAttn(_ x: MLXArray, _ p: String) -> MLXArray {
        let seq = x.dim(0)
        func heads(_ t: MLXArray) -> MLXArray {
            t.reshaped([1, seq, Self.HEADS, Self.HEAD_DIM]).transposed(0, 2, 1, 3)
        }
        let q = heads(lin(x, "\(p).q_proj"))
        let k = heads(lin(x, "\(p).k_proj"))
        let v = heads(lin(x, "\(p).v_proj"))
        let o = scaledDotProductAttention(queries: q, keys: k, values: v, scale: Self.SCALE, mask: nil)
            .transposed(0, 2, 1, 3).reshaped([seq, Self.H])
        return lin(o, "\(p).out_proj")
    }

    // Shared pre-LN transformer block: vision and text encoder layers are
    // architecturally identical here (this checkpoint's text tower was
    // configured to mirror the shape-optimized SO400M vision tower).
    private func block(_ x: MLXArray, _ p: String) -> MLXArray {
        var r = x
        var h = r + selfAttn(ln(r, "\(p).layer_norm1"), "\(p).self_attn")
        r = h
        var m = ln(h, "\(p).layer_norm2")
        m = lin(m, "\(p).mlp.fc1")
        m = geluApproximate(m)      // gelu_pytorch_tanh, matches act_kwargs.approximate="tanh"
        m = lin(m, "\(p).mlp.fc2")
        return r + m
    }

    // Multihead attention-pooling head (MAP): a single learned probe
    // cross-attends over the post-layernorm patch tokens. Fused QKV
    // in_proj: rows [0..<H) project the probe (Q), rows [H..<3H) project the
    // patch tokens into concatenated K;V (PyTorch nn.MultiheadAttention
    // convention, carried through mlx-embeddings' sanitize()).
    private func mapHead(_ x: MLXArray) -> MLXArray {
        let probe = w("vision_model.vision_model.head.probe").reshaped([1, Self.H])
        let inW = w("vision_model.vision_model.head.attention.in_proj.weight")
        let inB = w("vision_model.vision_model.head.attention.in_proj.bias")
        let qW = inW[0 ..< Self.H], kvW = inW[Self.H ..< (3 * Self.H)]
        let qB = inB[0 ..< Self.H], kvB = inB[Self.H ..< (3 * Self.H)]

        let q = matmul(probe, qW.transposed()) + qB
        let kv = matmul(x, kvW.transposed()) + kvB
        let k = kv[.ellipsis, 0 ..< Self.H]
        let v = kv[.ellipsis, Self.H ..< (2 * Self.H)]

        func heads(_ t: MLXArray, _ seq: Int) -> MLXArray {
            t.reshaped([1, seq, Self.HEADS, Self.HEAD_DIM]).transposed(0, 2, 1, 3)
        }
        let qh = heads(q, 1), kh = heads(k, Self.NUM_PATCHES), vh = heads(v, Self.NUM_PATCHES)
        var o = scaledDotProductAttention(queries: qh, keys: kh, values: vh, scale: Self.SCALE, mask: nil)
            .transposed(0, 2, 1, 3).reshaped([1, Self.H])
        o = lin(o, "vision_model.vision_model.head.attention.out_proj")

        let residual = o
        var hs = ln(o, "vision_model.vision_model.head.layernorm")
        hs = lin(hs, "vision_model.vision_model.head.mlp.fc1")
        hs = geluApproximate(hs)
        hs = lin(hs, "vision_model.vision_model.head.mlp.fc2")
        return residual + hs
    }

    // resize_mode=squash (preprocess_cfg.json): direct bicubic resize to
    // 384x384, no aspect-preserving crop. Patch flatten order (row, col,
    // channel) matches this checkpoint's MLX-native conv weight layout
    // [out=1152, kH=16, kW=16, in=3] (already transposed from PyTorch's
    // [out,in,kH,kW] by the mlx-community conversion) — which happens to be
    // the same contiguous order as the source RGB buffer, so no shuffle
    // beyond the patch loop itself is needed.
    private func patches(_ cg: CGImage) -> MLXArray {
        let (full, iw, ih) = rgbBuffer(cg)
        let resized = (iw == Self.IMG && ih == Self.IMG)
            ? full : Resize.bicubic(full, w: iw, h: ih, outW: Self.IMG, outH: Self.IMG)
        let nP = Self.GRID, dim = 3 * Self.PATCH * Self.PATCH
        var flat = [Float](repeating: 0, count: nP * nP * dim)
        for pi in 0 ..< nP {
            for pj in 0 ..< nP {
                let p = pi * nP + pj
                for i in 0 ..< Self.PATCH {
                    for j in 0 ..< Self.PATCH {
                        let py = pi * Self.PATCH + i, px = pj * Self.PATCH + j
                        for c in 0 ..< 3 {
                            let v = Float(resized[(py * Self.IMG + px) * 3 + c]) / 255.0
                            flat[p * dim + i * Self.PATCH * 3 + j * 3 + c] = (v - 0.5) / 0.5
                        }
                    }
                }
            }
        }
        return MLXArray(flat, [nP * nP, dim])
    }

    // Scoped to a GPU stream for just this call (Stream.withNewDefaultStream
    // sets a @TaskLocal, not the process-wide default) — SO400M is a ~28x
    // slower embed on CPU (17s vs 0.8s measured on-device), but the rest of
    // the service (default mlx CLIP path, Vision-framework OCR/face
    // detection) must keep running on whatever the global default is.
    // Concurrency: this service has no gpu_lock equivalent to the Python
    // fork's (README's documented MLX-vs-Vision-framework Metal crash
    // mitigation) — stress-tested manually with 60 concurrent mixed
    // clip/facial-recognition/ocr requests (2 bursts of ~20, some combined
    // in one request) with no crash and flat memory, but that is not the
    // same guarantee as the real ml-preflight.py gate and this hasn't run
    // through it.
    func embedVisual(_ cg: CGImage) -> [Float] {
        Stream.withNewDefaultStream(device: Device(.gpu)) {
            let wPatch = w("vision_model.vision_model.embeddings.patch_embedding.weight")
                .reshaped([Self.H, 3 * Self.PATCH * Self.PATCH])
            var x = matmul(patches(cg), wPatch.transposed())
            x = x + w("vision_model.vision_model.embeddings.patch_embedding.bias")
            x = x + w("vision_model.vision_model.embeddings.position_embedding.weight")
            for l in 0 ..< Self.LAYERS {
                x = block(x, "vision_model.vision_model.encoder.layers.\(l)")
            }
            x = ln(x, "vision_model.vision_model.post_layernorm")
            var emb = mapHead(x).reshaped([Self.H])
            emb = emb / sqrt((emb * emb).sum())
            eval(emb)
            return emb.asArray(Float.self)
        }
    }

    // ids: already tokenized (canonicalize + encode + EOS-preserving
    // truncate + pad to context_length), matching ZooCLIP's existing
    // embedTextual pipeline. Pools the fixed last position, unmasked — the
    // ONNX graph itself takes no attention_mask input for this model
    // (single-input textual tower), relying on non-causal self-attention to
    // let the last position see the whole real sequence regardless of
    // trailing padding ("sticky EOS" convention).
    func embedTextual(_ ids: [Int]) -> [Float] {
        Stream.withNewDefaultStream(device: Device(.gpu)) {
            let seq = ids.count
            let tokEmb = w("text_model.text_model.embeddings.token_embedding.weight")
            let posEmb = w("text_model.text_model.embeddings.position_embedding.weight")
            let idsArr = MLXArray(ids.map { Int32($0) })
            var x = take(tokEmb, idsArr, axis: 0) + posEmb[0 ..< seq]
            for l in 0 ..< Self.LAYERS {
                x = block(x, "text_model.text_model.encoder.layers.\(l)")
            }
            x = ln(x, "text_model.text_model.final_layer_norm")
            let pooled = x[seq - 1].reshaped([1, Self.H])
            var emb = lin(pooled, "text_model.text_model.head").reshaped([Self.H])
            emb = emb / sqrt((emb * emb).sum())
            eval(emb)
            return emb.asArray(Float.self)
        }
    }

    // MARK: - weight fetch

    static let weightsDir = NATIVE_CACHE_DIR.appendingPathComponent("mlx-siglip2-so400m")

    // One safetensors file (~2.3GB, fp16); a framework-agnostic format mlx-swift
    // loads directly, so no conversion step is needed on-device.
    static func ensureWeights() throws -> String {
        let dst = weightsDir.appendingPathComponent("model.safetensors")
        if ((try? dst.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) ?? 0 > 0 {
            return dst.path
        }
        try FileManager.default.createDirectory(at: weightsDir, withIntermediateDirectories: true)
        let url = URL(string: "https://huggingface.co/\(hfRepo)/resolve/main/model.safetensors")!
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 60
        cfg.timeoutIntervalForResource = 3600
        let session = URLSession(configuration: cfg)

        var lastError = ""
        for attempt in 1...3 {
            let sem = DispatchSemaphore(value: 0)
            var result: URL?
            var status = 0
            var transportError: Error?
            let task = session.downloadTask(with: url) { tmp, resp, err in
                if let tmp {
                    let hold = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
                    try? FileManager.default.moveItem(at: tmp, to: hold)
                    result = hold
                }
                status = (resp as? HTTPURLResponse)?.statusCode ?? 0
                transportError = err
                sem.signal()
            }
            print("[native-ml] fetching \(hfRepo)/model.safetensors (~2.3GB, one-time)")
            task.resume()
            sem.wait()

            if status == 200, let tmp = result {
                try? FileManager.default.removeItem(at: dst)
                try FileManager.default.moveItem(at: tmp, to: dst)
                return dst.path
            }
            lastError = "HTTP \(status)" + (transportError.map { ", \($0.localizedDescription)" } ?? "")
            if attempt < 3 {
                print("[native-ml] retrying siglip2 weight fetch (\(lastError))")
                Thread.sleep(forTimeInterval: Double(attempt) * 2)
            }
        }
        throw PredictError(status: "422 Unprocessable Entity",
                           message: "siglip2 weights: download failed (\(lastError))")
    }
}
