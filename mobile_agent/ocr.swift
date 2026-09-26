import Foundation
import Vision

// Reads one image on stdin. Output rects use normalized, top-left coordinates.
do {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .fast
    request.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(data: data, options: [:])
    try handler.perform([request])
    let rows: [[String: Any]] = (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return ["text": candidate.string, "confidence": candidate.confidence,
                "rect": [box.minX, 1 - box.maxY, box.width, box.height]]
    }
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: rows))
} catch {
    FileHandle.standardError.write(Data("OCR failed\n".utf8))
    exit(1)
}
