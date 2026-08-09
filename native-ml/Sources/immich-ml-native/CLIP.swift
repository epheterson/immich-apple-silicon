import Foundation
import CoreGraphics
import MLX

// CLIP ViT-B-32 image encoder (mlx-swift). Proven bit-identical to the Python
// mlx_clip path (cosine 1.0). Loads the same mlx-community safetensors.
final class CLIPEncoder {
    static let H = 768, LAYERS = 12, HEADS = 12, PATCH = 32, IMG = 224
    static let HEAD_DIM = H / HEADS
    static let SCALE = Float(1.0) / Float(HEAD_DIM).squareRoot()
    static let EPS: Float = 1e-5
    static let MEAN: [Float] = [0.48145466, 0.4578275, 0.40821073]
    static let STD: [Float] = [0.26862954, 0.26130258, 0.27577711]

    let W: [String: MLXArray]
    init(modelDir: String) {
        let sf = (try? FileManager.default.contentsOfDirectory(atPath: modelDir))?
            .first { $0.hasSuffix(".safetensors") }!
        W = try! MLX.loadArrays(url: URL(fileURLWithPath: modelDir + "/" + sf!))
    }
    private func w(_ k: String) -> MLXArray { W[k]! }

    // CGImage -> normalized patch matrix [49, 3072]. Matches mlx_clip's
    // img_processor: PIL-style bicubic resize (short side -> 224) then center
    // crop 224, rescale /255, normalize. Resize filter parity matters for CLIP.
    private func patches(_ cg: CGImage) -> MLXArray {
        let S = Self.IMG
        let (full, w, h) = rgbBuffer(cg)
        let short = min(w, h)
        let newW = w <= h ? S : Int(Double(S) * Double(w) / Double(short))   // int() truncation, like mlx_clip
        let newH = w <= h ? Int(Double(S) * Double(h) / Double(short)) : S
        let resized = (newW == w && newH == h) ? full : Resize.bicubic(full, w: w, h: h, outW: newW, outH: newH)
        let left = (newW - S) / 2, top = (newH - S) / 2   // center crop 224x224
        func px(_ c: Int, _ y: Int, _ x: Int) -> Float {
            (Float(resized[((top + y) * newW + (left + x)) * 3 + c]) / 255.0 - Self.MEAN[c]) / Self.STD[c]
        }
        let nP = S / Self.PATCH, dim = 3 * Self.PATCH * Self.PATCH
        var flat = [Float](repeating: 0, count: nP * nP * dim)
        for pi in 0..<nP { for pj in 0..<nP {
            let p = pi * nP + pj
            for c in 0..<3 { for i in 0..<Self.PATCH { for j in 0..<Self.PATCH {
                flat[p * dim + c * Self.PATCH * Self.PATCH + i * Self.PATCH + j] =
                    px(c, pi * Self.PATCH + i, pj * Self.PATCH + j)
            }}}
        }}
        return MLXArray(flat, [nP * nP, dim])
    }

    private func ln(_ x: MLXArray, _ g: MLXArray, _ b: MLXArray) -> MLXArray {
        (x - mean(x, axis: -1, keepDims: true)) * rsqrt(variance(x, axis: -1, keepDims: true) + Self.EPS) * g + b
    }
    private func lin(_ x: MLXArray, _ wt: MLXArray, _ b: MLXArray?) -> MLXArray {
        let y = matmul(x, wt.transposed()); return b == nil ? y : y + b!
    }
    private func attn(_ x: MLXArray, _ p: String) -> MLXArray {
        let seq = x.dim(0)
        func heads(_ t: MLXArray) -> MLXArray { t.reshaped([seq, Self.HEADS, Self.HEAD_DIM]).transposed(1, 0, 2) }
        let q = heads(lin(x, w("\(p).q_proj.weight"), w("\(p).q_proj.bias")))
        let k = heads(lin(x, w("\(p).k_proj.weight"), w("\(p).k_proj.bias")))
        let v = heads(lin(x, w("\(p).v_proj.weight"), w("\(p).v_proj.bias")))
        let s = softmax(matmul(q, k.transposed(0, 2, 1)) * Self.SCALE, axis: -1)
        let o = matmul(s, v).transposed(1, 0, 2).reshaped([seq, Self.H])
        return lin(o, w("\(p).out_proj.weight"), w("\(p).out_proj.bias"))
    }

    // Concurrency: guarded by metalLock (GPULock.swift) — see that file.
    func embed(_ cg: CGImage) -> [Float] {
        withMetalLock {
            let wPatch = w("vision_model.embeddings.patch_embedding.weight").reshaped([Self.H, 3 * Self.PATCH * Self.PATCH])
            var x = matmul(patches(cg), wPatch.transposed())
            x = concatenated([w("vision_model.embeddings.class_embedding").reshaped([1, Self.H]), x], axis: 0)
            x = x + w("vision_model.embeddings.position_embedding.weight")
            x = ln(x, w("vision_model.pre_layrnorm.weight"), w("vision_model.pre_layrnorm.bias"))
            for l in 0..<Self.LAYERS {
                let p = "vision_model.encoder.layers.\(l)"
                var r = x
                x = r + attn(ln(x, w("\(p).layer_norm1.weight"), w("\(p).layer_norm1.bias")), "\(p).self_attn")
                r = x
                var h = ln(x, w("\(p).layer_norm2.weight"), w("\(p).layer_norm2.bias"))
                h = lin(h, w("\(p).mlp.fc1.weight"), w("\(p).mlp.fc1.bias"))
                h = h * sigmoid(1.702 * h)   // quick_gelu
                h = lin(h, w("\(p).mlp.fc2.weight"), w("\(p).mlp.fc2.bias"))
                x = r + h
            }
            let pooled = ln(x[0].reshaped([1, Self.H]),
                            w("vision_model.post_layernorm.weight"),
                            w("vision_model.post_layernorm.bias"))
            let emb = matmul(pooled, w("visual_projection.weight").transposed()).reshaped([512])
            let e = emb / sqrt((emb * emb).sum())
            eval(e)
            return e.asArray(Float.self)
        }
    }
}
