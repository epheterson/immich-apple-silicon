import Foundation
import Vision
import CoreGraphics

// OCR + face detection via Apple's Vision framework — the same engine the Python
// ML service reaches through PyObjC, but native. No model files, no venv.

struct OCRLine { let text: String; let confidence: Float }

func runOCR(_ cg: CGImage) -> [OCRLine] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([req])
    return (req.results ?? []).compactMap {
        guard let t = $0.topCandidates(1).first else { return nil }
        return OCRLine(text: t.string, confidence: t.confidence)
    }
}

struct FaceBox { let x: Double, y: Double, w: Double, h: Double, confidence: Double }

func detectFaces(_ cg: CGImage) -> [FaceBox] {
    let req = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([req])
    return (req.results ?? []).map {
        FaceBox(x: $0.boundingBox.minX, y: $0.boundingBox.minY,
                w: $0.boundingBox.width, h: $0.boundingBox.height,
                confidence: Double($0.confidence))
    }
}

func loadCGImage(_ path: String) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}
func loadCGImage(data: Data) -> CGImage? {
    guard let src = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(src, 0, nil)
}
