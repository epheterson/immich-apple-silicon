import Foundation
import Vision

// OCR in Immich's response format, matching the Python fork's ocr.recognize_text:
// VNRecognizeTextRequest (accurate, language correction), normalized 8-coord
// boxes (TL,TR,BR,BL, Y from top), per-text scores. Fed raw image bytes like the
// Python path (VNImageRequestHandler initWithData).
func recognizeTextImmich(imageData: Data, minScore: Float, languageCorrection: Bool) -> [String: Any] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = languageCorrection
    let handler = VNImageRequestHandler(data: imageData, options: [:])
    try? handler.perform([req])

    var texts: [String] = [], boxes: [Double] = [], boxScores: [Double] = [], textScores: [Double] = []
    for obs in (req.results ?? []) {
        guard let cand = obs.topCandidates(1).first else { continue }
        let conf = cand.confidence
        if conf < minScore { continue }
        texts.append(cand.string)
        textScores.append(Double(conf))
        let bb = obs.boundingBox
        let x = Double(bb.origin.x)
        let y = 1.0 - Double(bb.origin.y) - Double(bb.height)   // flip to top-origin
        let w = Double(bb.width), h = Double(bb.height)
        boxes.append(contentsOf: [x, y, x + w, y, x + w, y + h, x, y + h])   // TL,TR,BR,BL
        boxScores.append(Double(obs.confidence))
    }
    return ["text": texts, "box": boxes, "boxScore": boxScores, "textScore": textScores]
}
