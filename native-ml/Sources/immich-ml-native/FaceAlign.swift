import Foundation
import CoreGraphics

// InsightFace norm_crop: 5-point similarity align to the ArcFace template, then
// bilinear warp to 112x112. Matches insightface.utils.face_align.norm_crop.
// The 2D similarity least-squares below is equivalent to skimage's Umeyama for
// the non-reflection case (real face landmarks never require a mirror).
enum FaceAlign {
    static let dst: [[Double]] = [
        [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
        [41.5493, 92.3655], [70.7299, 92.2041],
    ]

    // src(5x2)->dst(5x2) similarity. Returns 2x3 affine [[a,b,tx],[c,d,ty]].
    static func similarity(_ src: [[Double]], _ dst: [[Double]]) -> [[Double]] {
        let n = src.count
        var mS = [0.0, 0.0], mD = [0.0, 0.0]
        for i in 0..<n { mS[0] += src[i][0]; mS[1] += src[i][1]; mD[0] += dst[i][0]; mD[1] += dst[i][1] }
        mS[0] /= Double(n); mS[1] /= Double(n); mD[0] /= Double(n); mD[1] /= Double(n)

        var m00 = 0.0, m01 = 0.0, m10 = 0.0, m11 = 0.0, varS = 0.0   // M2 = Σ b·aᵀ (demeaned)
        for i in 0..<n {
            let ax = src[i][0] - mS[0], ay = src[i][1] - mS[1]
            let bx = dst[i][0] - mD[0], by = dst[i][1] - mD[1]
            m00 += bx * ax; m01 += bx * ay; m10 += by * ax; m11 += by * ay
            varS += ax * ax + ay * ay
        }
        let theta = atan2(m10 - m01, m00 + m11)
        let c = cos(theta), s = sin(theta)
        var num = 0.0   // scale = Σ⟨b, R a⟩ / Σ|a|²
        for i in 0..<n {
            let ax = src[i][0] - mS[0], ay = src[i][1] - mS[1]
            let bx = dst[i][0] - mD[0], by = dst[i][1] - mD[1]
            num += bx * (c * ax - s * ay) + by * (s * ax + c * ay)
        }
        let scale = num / varS
        let a = scale * c, b = -scale * s, cc = scale * s, d = scale * c
        return [[a, b, mD[0] - (a * mS[0] + b * mS[1])],
                [cc, d, mD[1] - (cc * mS[0] + d * mS[1])]]
    }

    static func invert(_ M: [[Double]]) -> [[Double]] {
        let a = M[0][0], b = M[0][1], tx = M[0][2], c = M[1][0], d = M[1][1], ty = M[1][2]
        let det = a * d - b * c
        let ia = d / det, ib = -b / det, ic = -c / det, id = a / det
        return [[ia, ib, -(ia * tx + ib * ty)], [ic, id, -(ic * tx + id * ty)]]
    }

    // src RGB (row-major w*h*3) -> aligned size×size RGB, bilinear, border 0.
    static func normCrop(rgb: [UInt8], w: Int, h: Int, landmarks: [[Double]], size: Int = 112) -> [UInt8] {
        let inv = invert(similarity(landmarks, dst))
        var out = [UInt8](repeating: 0, count: size * size * 3)
        for yo in 0..<size {
            for xo in 0..<size {
                let xs = inv[0][0] * Double(xo) + inv[0][1] * Double(yo) + inv[0][2]
                let ys = inv[1][0] * Double(xo) + inv[1][1] * Double(yo) + inv[1][2]
                let x0 = Int(floor(xs)), y0 = Int(floor(ys))
                let fx = xs - Double(x0), fy = ys - Double(y0)
                for ch in 0..<3 {
                    func px(_ xx: Int, _ yy: Int) -> Double {
                        (xx < 0 || yy < 0 || xx >= w || yy >= h) ? 0 : Double(rgb[(yy * w + xx) * 3 + ch])
                    }
                    let v = px(x0, y0) * (1 - fx) * (1 - fy) + px(x0 + 1, y0) * fx * (1 - fy)
                        + px(x0, y0 + 1) * (1 - fx) * fy + px(x0 + 1, y0 + 1) * fx * fy
                    out[(yo * size + xo) * 3 + ch] = UInt8(max(0, min(255, v.rounded())))
                }
            }
        }
        return out
    }
}

// Native-resolution RGB byte buffer from a CGImage (drops alpha).
func rgbBuffer(_ cg: CGImage) -> ([UInt8], Int, Int) {
    let w = cg.width, h = cg.height
    var rgba = [UInt8](repeating: 0, count: w * h * 4)
    let ctx = CGContext(data: &rgba, width: w, height: h, bitsPerComponent: 8,
                        bytesPerRow: w * 4, space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.draw(cg, in: CGRect(x: 0, y: 0, width: w, height: h))
    var rgb = [UInt8](repeating: 0, count: w * h * 3)
    for i in 0..<(w * h) { rgb[i * 3] = rgba[i * 4]; rgb[i * 3 + 1] = rgba[i * 4 + 1]; rgb[i * 3 + 2] = rgba[i * 4 + 2] }
    return (rgb, w, h)
}
