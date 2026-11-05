import Foundation
import Vision

// The screen-text helper (native_helpers.find("screen-text")): reads one JPEG or PNG on stdin and prints the text
// Apple Vision finds as JSON rows {"text", "confidence", "rect"}, rects in screen fractions from the top left. The
// two-headed check (mobile_agent/screen_text.py) compares it with a control's accessibility label before a tap.
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
    FileHandle.standardError.write(Data("screen text failed\n".utf8))
    exit(1)
}
