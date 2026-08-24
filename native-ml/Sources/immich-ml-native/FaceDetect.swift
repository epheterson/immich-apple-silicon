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
    // Detect and landmark in two stages, because the landmarks request is a
    // worse detector than the rectangles request and reports a useless score.
    //
    // Asking VNDetectFaceLandmarksRequest to do both, which is what this did,
    // costs faces outright: measured across 24 images it found nothing on
    // several where the rectangles request finds a face, and against Immich's
    // own detector it found 20 of 48 faces. It also reported confidence 1.000
    // for every face ever returned, so Immich's minScore had nothing to filter
    // on. The rectangles request returns real values (0.610, 0.654, 0.879 on
    // the same images).
    //
    // inputFaceObservations is the supported way to join them: the landmarks
    // pass runs on exactly the observations the detector found, so the 5 points
    // the aligner needs still come back for every face, and the confidence
    // survives.
    let rects = VNDetectFaceRectanglesRequest()
    // Raw image bytes, not a pre-decoded CGImage: the Python fork feeds
    // VNImageRequestHandler initWithData and Vision produces slightly
    // different landmarks otherwise.
    try? VNImageRequestHandler(data: imageData, options: [:]).perform([rects])
    let detected = (rects.results as? [VNFaceObservation]) ?? []
    guard !detected.isEmpty else { return [] }

    let req = VNDetectFaceLandmarksRequest()
    req.inputFaceObservations = detected
    try? VNImageRequestHandler(data: imageData, options: [:]).perform([req])
    // Falling back to the detections rather than dropping them: a face without
    // landmarks is still a face, and losing it here would undo the point.
    let observations = (req.results as? [VNFaceObservation]) ?? detected

    var faces: [DetectedFace] = []
    for obs in observations {
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
