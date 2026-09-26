import Foundation
import Vision

// Local tier 0 for mobile_agent/vision_judge.py (AppleVisionScreen).
// Long-lived: reads frames from stdin as a 4-byte big-endian length followed by
// encoded image bytes (JPEG/PNG), and writes one JSON line per frame:
//   {"labels": {"dog": 0.93, ...}, "animals": {"Dog": 0.88}, "faces": 1, "ms": 4.1}
// A zero length ends the process. Nothing is sent over the network.
// Build: python -m mobile_agent.vision_judge build-t0  (-> mobile_agent/.build/vision-t0)

let input = FileHandle.standardInput
let output = FileHandle.standardOutput

func readExactly(_ count: Int) -> Data? {
    var data = Data()
    while data.count < count {
        let chunk = input.readData(ofLength: count - data.count)
        if chunk.isEmpty { return nil }
        data.append(chunk)
    }
    return data
}

func emit(_ object: [String: Any]) {
    guard var line = try? JSONSerialization.data(withJSONObject: object) else { return }
    line.append(0x0A)
    output.write(line)
}

while let header = readExactly(4) {
    let length = header.reduce(0) { ($0 << 8) | Int($1) }
    if length == 0 || length > 20_000_000 { break }
    guard let frame = readExactly(length) else { break }
    let started = DispatchTime.now()
    let classify = VNClassifyImageRequest()
    let animals = VNRecognizeAnimalsRequest()
    let faces = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(data: frame, options: [:])
    do {
        try handler.perform([classify, animals, faces])
        var labels: [String: Double] = [:]
        for observation in (classify.results ?? []) where observation.confidence >= 0.01 {
            labels[observation.identifier] = Double(observation.confidence)
        }
        var found: [String: Double] = [:]
        for observation in (animals.results ?? []) {
            for label in observation.labels {
                found[label.identifier] = max(found[label.identifier] ?? 0, Double(label.confidence))
            }
        }
        let ms = Double(DispatchTime.now().uptimeNanoseconds - started.uptimeNanoseconds) / 1_000_000
        emit(["labels": labels, "animals": found, "faces": faces.results?.count ?? 0, "ms": ms])
    } catch {
        emit(["error": "vision_failed"])
    }
}
