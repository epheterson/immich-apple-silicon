import Foundation

// PIL-compatible bicubic resize (separable, antialiased on downscale) to match
// mlx_clip's img_processor (PIL Image.resize default = bicubic). CGContext uses a
// different filter, which perturbs CLIP embeddings; this recovers the parity.
enum Resize {
    // cubic convolution kernel, a = -0.5 (PIL/Catmull variant used by BICUBIC)
    private static func cubic(_ x: Double) -> Double {
        let a = -0.5
        let ax = abs(x)
        if ax < 1 { return ((a + 2) * ax - (a + 3)) * ax * ax + 1 }
        if ax < 2 { return (((ax - 5) * ax + 8) * ax - 4) * a }
        return 0
    }

    // Precompute PIL-style weight bins for one axis: for each output index, the
    // source range [min,max) and normalized weights.
    private static func weights(_ inSize: Int, _ outSize: Int) -> [(min: Int, w: [Double])] {
        let scale = Double(inSize) / Double(outSize)
        let filterscale = max(1.0, scale)
        let support = 2.0 * filterscale
        let ss = 1.0 / filterscale
        var bins: [(Int, [Double])] = []
        bins.reserveCapacity(outSize)
        for xx in 0..<outSize {
            let center = (Double(xx) + 0.5) * scale
            var xmin = Int(center - support + 0.5)
            if xmin < 0 { xmin = 0 }
            var xmax = Int(center + support + 0.5)
            if xmax > inSize { xmax = inSize }
            var w = [Double](); var total = 0.0
            for x in xmin..<xmax {
                let wt = cubic((Double(x) - center + 0.5) * ss)
                w.append(wt); total += wt
            }
            if total != 0 { for i in 0..<w.count { w[i] /= total } }
            bins.append((xmin, w))
        }
        return bins
    }

    // RGB (row-major, w*h*3) -> outW×outH RGB. Horizontal pass then vertical,
    // matching PIL's two-pass ImagingResample. Rounds to nearest, clamps 0..255.
    static func bicubic(_ rgb: [UInt8], w: Int, h: Int, outW: Int, outH: Int) -> [UInt8] {
        let hx = weights(w, outW)
        // horizontal: w×h -> outW×h (float)
        var tmp = [Double](repeating: 0, count: outW * h * 3)
        for y in 0..<h {
            for ox in 0..<outW {
                let (xmin, ws) = hx[ox]
                var acc = [0.0, 0.0, 0.0]
                for (k, wt) in ws.enumerated() {
                    let sx = xmin + k
                    let base = (y * w + sx) * 3
                    acc[0] += Double(rgb[base]) * wt
                    acc[1] += Double(rgb[base + 1]) * wt
                    acc[2] += Double(rgb[base + 2]) * wt
                }
                let o = (y * outW + ox) * 3
                tmp[o] = acc[0]; tmp[o + 1] = acc[1]; tmp[o + 2] = acc[2]
            }
        }
        // vertical: outW×h -> outW×outH
        let hy = weights(h, outH)
        var out = [UInt8](repeating: 0, count: outW * outH * 3)
        for oy in 0..<outH {
            let (ymin, ws) = hy[oy]
            for ox in 0..<outW {
                var acc = [0.0, 0.0, 0.0]
                for (k, wt) in ws.enumerated() {
                    let sy = ymin + k
                    let base = (sy * outW + ox) * 3
                    acc[0] += tmp[base] * wt
                    acc[1] += tmp[base + 1] * wt
                    acc[2] += tmp[base + 2] * wt
                }
                let o = (oy * outW + ox) * 3
                for c in 0..<3 { out[o + c] = UInt8(max(0, min(255, (acc[c]).rounded()))) }
            }
        }
        return out
    }
}
