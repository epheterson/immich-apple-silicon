import Foundation
import MLX

// CLIP ViT-B-32 text encoder (mlx-swift). Matches Python mlx_clip encode_text:
// token+position embeddings, 12-layer causal transformer (width 512, 8 heads,
// quick_gelu), final LN, EOT-position pooling, text_projection, L2-normalize.
// Loads the same safetensors as the visual encoder (text_model.* keys).
final class CLIPText {
    static let H = 512, LAYERS = 12, HEADS = 8, CTX = 77
    static let HEAD_DIM = H / HEADS                       // 64
    static let SCALE = Float(1.0) / Float(HEAD_DIM).squareRoot()
    static let EPS: Float = 1e-5

    let W: [String: MLXArray]
    let tok: CLIPTokenizer

    init(modelDir: String, tokenizer: CLIPTokenizer) {
        let sf = (try? FileManager.default.contentsOfDirectory(atPath: modelDir))?
            .first { $0.hasSuffix(".safetensors") }!
        W = try! MLX.loadArrays(url: URL(fileURLWithPath: modelDir + "/" + sf!))
        tok = tokenizer
    }
    private func w(_ k: String) -> MLXArray { W[k]! }

    private func ln(_ x: MLXArray, _ g: MLXArray, _ b: MLXArray) -> MLXArray {
        (x - mean(x, axis: -1, keepDims: true)) * rsqrt(variance(x, axis: -1, keepDims: true) + Self.EPS) * g + b
    }
    private func lin(_ x: MLXArray, _ wt: MLXArray, _ b: MLXArray?) -> MLXArray {
        let y = matmul(x, wt.transposed()); return b == nil ? y : y + b!
    }
    private func attn(_ x: MLXArray, _ p: String, _ causal: MLXArray) -> MLXArray {
        let seq = x.dim(0)
        func heads(_ t: MLXArray) -> MLXArray { t.reshaped([seq, Self.HEADS, Self.HEAD_DIM]).transposed(1, 0, 2) }
        let q = heads(lin(x, w("\(p).q_proj.weight"), w("\(p).q_proj.bias")))
        let k = heads(lin(x, w("\(p).k_proj.weight"), w("\(p).k_proj.bias")))
        let v = heads(lin(x, w("\(p).v_proj.weight"), w("\(p).v_proj.bias")))
        var scores = matmul(q, k.transposed(0, 2, 1)) * Self.SCALE   // [HEADS, seq, seq]
        scores = scores + causal                                    // broadcast [seq, seq]
        let s = softmax(scores, axis: -1)
        let o = matmul(s, v).transposed(1, 0, 2).reshaped([seq, Self.H])
        return lin(o, w("\(p).out_proj.weight"), w("\(p).out_proj.bias"))
    }

    // Concurrency: guarded by metalLock (GPULock.swift) — see that file.
    func encode(_ text: String) -> [Float] {
        withMetalLock {
            var ids = tok.encode(text)
            if ids.count > Self.CTX { ids = Array(ids.prefix(Self.CTX - 1)) + [CLIPTokenizer.EOT] }
            let seq = ids.count
            let eotPos = seq - 1   // EOT (49407, the max id) is always last

            let tokEmb = w("text_model.embeddings.token_embedding.weight")    // [49408, 512]
            let posEmb = w("text_model.embeddings.position_embedding.weight")  // [77, 512]
            let idsArr = MLXArray(ids.map { Int32($0) })
            let posIdx = MLXArray((0..<seq).map { Int32($0) })
            var x = take(tokEmb, idsArr, axis: 0) + take(posEmb, posIdx, axis: 0)

            // causal mask [seq, seq]: 0 on/below diagonal, large-negative above
            var maskFlat = [Float](repeating: 0, count: seq * seq)
            for i in 0..<seq { for j in (i + 1)..<seq { maskFlat[i * seq + j] = -1e9 } }
            let causal = MLXArray(maskFlat, [seq, seq])

            for l in 0..<Self.LAYERS {
                let p = "text_model.encoder.layers.\(l)"
                var r = x
                x = r + attn(ln(x, w("\(p).layer_norm1.weight"), w("\(p).layer_norm1.bias")), "\(p).self_attn", causal)
                r = x
                var h = ln(x, w("\(p).layer_norm2.weight"), w("\(p).layer_norm2.bias"))
                h = lin(h, w("\(p).mlp.fc1.weight"), w("\(p).mlp.fc1.bias"))
                h = h * sigmoid(1.702 * h)   // quick_gelu
                h = lin(h, w("\(p).mlp.fc2.weight"), w("\(p).mlp.fc2.bias"))
                x = r + h
            }
            x = ln(x, w("text_model.final_layer_norm.weight"), w("text_model.final_layer_norm.bias"))
            let pooled = x[eotPos].reshaped([1, Self.H])
            let emb = matmul(pooled, w("text_projection.weight").transposed()).reshaped([Self.H])
            let e = emb / sqrt((emb * emb).sum())
            eval(e)
            return e.asArray(Float.self)
        }
    }
}
