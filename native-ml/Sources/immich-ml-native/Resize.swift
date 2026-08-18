import Foundation

// PIL-compatible bicubic resize (separable, antialiased on downscale) to match
// mlx_clip's img_processor (PIL Image.resize default = bicubic). CGContext uses a
// different filter, which perturbs CLIP embeddings; this recovers the parity.
//
// Float (not Double) accumulation and row-parallel DispatchQueue.concurrentPerform:
// this runs on every /predict call, on the full source image resolution (not the
// small resize target), and was measured as a real chunk of end-to-end latency on
// realistic ~4032x3024 photos — see scripts/native-ml-full-benchmark.py's
// docstring (246ms vs 552ms, small vs realistic test image). Output stays within
// UInt8 rounding of the previous Double version; PIL itself has no stronger
// precision guarantee than that.
enum Resize {
    // cubic convolution kernel, a = -0.5 (PIL/Catmull variant used by BICUBIC)
    private static func cubic(_ x: Float) -> Float {
        let a: Float = -0.5
        let ax = abs(x)
        if ax < 1 { return ((a + 2) * ax - (a + 3)) * ax * ax + 1 }
        if ax < 2 { return (((ax - 5) * ax + 8) * ax - 4) * a }
        return 0
    }

    // Precompute PIL-style weight bins for one axis: for each output index, the
    // source range [min,max) and normalized weights.
    private static func weights(_ inSize: Int, _ outSize: Int) -> [(min: Int, w: [Float])] {
        let scale = Float(inSize) / Float(outSize)
        let filterscale = max(1.0, scale)
        let support = 2.0 * filterscale
        let ss = 1.0 / filterscale
        var bins: [(Int, [Float])] = []
        bins.reserveCapacity(outSize)
        for xx in 0..<outSize {
            let center = (Float(xx) + 0.5) * scale
            var xmin = Int(center - support + 0.5)
            if xmin < 0 { xmin = 0 }
            var xmax = Int(center + support + 0.5)
            if xmax > inSize { xmax = inSize }
            var w = [Float](); var total: Float = 0
            for x in xmin..<xmax {
                let wt = cubic((Float(x) - center + 0.5) * ss)
                w.append(wt); total += wt
            }
            if total != 0 { for i in 0..<w.count { w[i] /= total } }
            bins.append((xmin, w))
        }
        return bins
    }

    // RGB (row-major, w*h*3) -> outW×outH RGB. Horizontal pass then vertical,
    // matching PIL's two-pass ImagingResample. Rounds to nearest, clamps 0..255.
    // Each pass is parallelized across independent output rows.
    static func bicubic(_ rgb: [UInt8], w: Int, h: Int, outW: Int, outH: Int) -> [UInt8] {
        let hx = weights(w, outW)
        // horizontal: w×h -> outW×h (float)
        var tmp = [Float](repeating: 0, count: outW * h * 3)
        rgb.withUnsafeBufferPointer { src in
            tmp.withUnsafeMutableBufferPointer { dst in
                DispatchQueue.concurrentPerform(iterations: h) { y in
                    for ox in 0..<outW {
                        let (xmin, ws) = hx[ox]
                        var acc0: Float = 0, acc1: Float = 0, acc2: Float = 0
                        for k in 0..<ws.count {
                            let base = (y * w + (xmin + k)) * 3
                            let wt = ws[k]
                            acc0 += Float(src[base]) * wt
                            acc1 += Float(src[base + 1]) * wt
                            acc2 += Float(src[base + 2]) * wt
                        }
                        let o = (y * outW + ox) * 3
                        dst[o] = acc0; dst[o + 1] = acc1; dst[o + 2] = acc2
                    }
                }
            }
        }
        // vertical: outW×h -> outW×outH
        let hy = weights(h, outH)
        var out = [UInt8](repeating: 0, count: outW * outH * 3)
        tmp.withUnsafeBufferPointer { src in
            out.withUnsafeMutableBufferPointer { dst in
                DispatchQueue.concurrentPerform(iterations: outH) { oy in
                    let (ymin, ws) = hy[oy]
                    for ox in 0..<outW {
                        var acc0: Float = 0, acc1: Float = 0, acc2: Float = 0
                        for k in 0..<ws.count {
                            let base = ((ymin + k) * outW + ox) * 3
                            let wt = ws[k]
                            acc0 += src[base] * wt
                            acc1 += src[base + 1] * wt
                            acc2 += src[base + 2] * wt
                        }
                        let o = (oy * outW + ox) * 3
                        dst[o] = UInt8(max(0, min(255, acc0.rounded())))
                        dst[o + 1] = UInt8(max(0, min(255, acc1.rounded())))
                        dst[o + 2] = UInt8(max(0, min(255, acc2.rounded())))
                    }
                }
            }
        }
        return out
    }
}
