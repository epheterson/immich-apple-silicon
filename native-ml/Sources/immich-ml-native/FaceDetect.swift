import Foundation
import Vision
import CoreGraphics

// Face detection + 5-point landmarks via Vision's VNDetectFaceLandmarksRequest,
// replicating the Python fork's face_detect exactly: pixel bbox (Y-flipped) and
// the same landmark picks (eye centers, nose last point, outer-lip x-extremes).
struct DetectedFace {
    let x1: Int, y1: Int, x2: Int, y2: Int
    let score: Float
    let landmarks: [[Double]]?   // [left_eye, right_eye, nose, left_mouth, right_mouth] pixels
}

func detectFacesWithLandmarks(imageData: Data, width W: Int, height H: Int) -> [DetectedFace] {
    let req = VNDetectFaceLandmarksRequest()
    // Match the Python fork: feed raw image bytes (VNImageRequestHandler initWithData),
    // not a pre-decoded CGImage — Vision produces slightly different landmarks otherwise.
    let handler = VNImageRequestHandler(data: imageData, options: [:])
    try? handler.perform([req])
    var faces: [DetectedFace] = []
    for obs in (req.results ?? []) {
        let bb = obs.boundingBox   // normalized, bottom-left origin
        let x1 = bb.origin.x * Double(W)
        let y1 = (1.0 - bb.origin.y - bb.height) * Double(H)
        let x2 = (bb.origin.x + bb.width) * Double(W)
        let y2 = (1.0 - bb.origin.y) * Double(H)
        let lm = obs.landmarks.flatMap { extractFive($0, bb: bb, W: W, H: H) }
        faces.append(DetectedFace(x1: Int(x1), y1: Int(y1), x2: Int(x2), y2: Int(y2),
                                  score: obs.confidence, landmarks: lm))
    }
    return faces
}

// landmark point (normalized within face bbox, bottom-left) -> image pixels
private func toImage(_ nx: Double, _ ny: Double, _ bb: CGRect, _ W: Int, _ H: Int) -> [Double] {
    let ix = Double(bb.origin.x) + nx * Double(bb.width)
    let iy = Double(bb.origin.y) + ny * Double(bb.height)
    return [ix * Double(W), (1.0 - iy) * Double(H)]
}

private func regionCenter(_ r: VNFaceLandmarkRegion2D?, _ bb: CGRect, _ W: Int, _ H: Int) -> [Double]? {
    guard let r = r, r.pointCount > 0 else { return nil }
    let pts = r.normalizedPoints
    var sx = 0.0, sy = 0.0
    for p in pts { sx += Double(p.x); sy += Double(p.y) }
    return toImage(sx / Double(pts.count), sy / Double(pts.count), bb, W, H)
}

private func extractFive(_ L: VNFaceLandmarks2D, bb: CGRect, W: Int, H: Int) -> [[Double]]? {
    guard let le = regionCenter(L.leftEye, bb, W, H),
          let re = regionCenter(L.rightEye, bb, W, H),
          let noseR = L.nose, noseR.pointCount > 0,
          let lips = L.outerLips, lips.pointCount > 0,
          let np = noseR.normalizedPoints.last
    else { return nil }
    let nose = toImage(Double(np.x), Double(np.y), bb, W, H)
    let lipsPx = lips.normalizedPoints.map { toImage(Double($0.x), Double($0.y), bb, W, H) }
    let leftMouth = lipsPx.min { $0[0] < $1[0] }!
    let rightMouth = lipsPx.max { $0[0] < $1[0] }!
    return [le, re, nose, leftMouth, rightMouth]
}
