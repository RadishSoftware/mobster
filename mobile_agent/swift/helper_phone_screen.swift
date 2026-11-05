// phone-screen: one USB iPhone's screen as live video on this Mac, read with Apple Vision, for Mobster's
// "Cable + Bluetooth" connection (mobile_agent/cable_bt.py). Experimental: used only with
// MOBSTER_EXPERIMENTAL_CABLE_BT=1. One long-lived process per phone; one capture session for its whole life.
//
// It reads one JSON request per line on standard input and prints one JSON line per request ({"id", "ok", ...}):
//   {"op":"open","udid":U,"name":N,"phones":K,"width":590}
//       Opens the iPhone's capture device (the one QuickTime Player records from) and starts its video, scaled in
//       hardware to `width` pixels wide: {"device":{"id","name","model"},"match","ms":{"find","open","first"}}.
//       The device is matched as phone-audio matches it: UDID, then the phone's name, then the only iOS one.
//   {"op":"capture","path":P,"ocr":true,"fast":false,"mask":0.064}
//       The newest frame as a JPEG at P, its pixel hash and share of near-black cells, and with "ocr" the text Vision
//       reads on it: {"rows":[{"text","confidence","rect":[x,y,w,h]}]}, rects as fractions from the top left.
//       "mask" blacks out that top fraction of the picture and drops the text read there: while the phone is
//       captured, iOS draws a demonstration status bar (9:41, a full battery) that isn't the phone's.
//       Also {"window":{"w","h"},"pixels":[w,h],"age":ms since the frame arrived,"fps","ms":{"capture","total"}}.
//   {"op":"mark"}       Keeps the newest frame as the reference for "blobs".
//   {"op":"blobs","threshold":28,"min":4}
//       What changed since "mark": [{"x","y","w","h","area"}] as fractions (centre x, y), largest first. The pointer
//       check uses it: AssistiveTouch's pointer is drawn into the captured video.
//   {"op":"frame","width":196,"quality":0.5,"after":KEY}
//       The live view's picture (mobile_agent/wda/frame_source.py): the newest frame scaled on the GPU to `width`, as an
//       inline base64 JPEG {"jpeg","key","w","h","age"}, or {"same":true,"key"} when its key is still `after`.
//
// Requests other than open run side by side, each answered with its own id.
//
// Failures are {"ok":false,"code","error"}: no_device, ambiguous_device, permission (camera access for the capture
// device), not_open, capture, usage. Nothing is sent to the phone: this helper only watches.
//
// Opening the capture device changes the phone while it runs: iOS shows the 9:41 status bar and routes its sound to
// this Mac.

import AVFoundation
import CoreImage
import CoreMediaIO
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

struct HelperError: Error {
    let code: String
    let message: String
}

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
       let line = String(data: data, encoding: .utf8) {
        FileHandle.standardOutput.write(Data((line + "\n").utf8))
    }
}

func ms(_ since: Date) -> Int { Int((Date().timeIntervalSince(since) * 1000).rounded()) }

// MARK: - Finding the iPhone (as helper_phone_audio.swift does)

func allowScreenCaptureDevices() {
    var address = CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
    var allow: UInt32 = 1
    CMIOObjectSetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &address, 0, nil,
                              UInt32(MemoryLayout<UInt32>.size), &allow)
}

func iphones() -> [AVCaptureDevice] {
    let types: [AVCaptureDevice.DeviceType]
    if #available(macOS 14.0, *) { types = [.external] } else { types = [.externalUnknown] }
    return AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .muxed, position: .unspecified).devices
}

func plain(_ id: String) -> String { id.replacingOccurrences(of: "-", with: "").uppercased() }

func folded(_ name: String) -> String {
    name.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        .folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
}

func looksLikeIOS(_ device: AVCaptureDevice) -> Bool {
    let model = device.modelID.lowercased()
    return model.contains("ios") || model.hasPrefix("iphone") || model.hasPrefix("ipad")
}

/// The capture device for the phone asked for: its UDID, else its name (when no other phone shares it), else the
/// only iOS capture device when the device layer sees one USB iPhone. Never a guess between two phones.
func pick(_ found: [AVCaptureDevice], udid: String?, name: String?, phones: Int?) throws -> (AVCaptureDevice, String)? {
    if let udid = udid, let device = found.first(where: { plain($0.uniqueID) == plain(udid) }) {
        return (device, "udid")
    }
    let ios = found.filter(looksLikeIOS)
    if let name = name, !folded(name).isEmpty {
        let named = ios.filter { folded($0.localizedName) == folded(name) }
        if named.count == 1 { return (named[0], "name") }
        if named.count > 1 {
            throw HelperError(code: "ambiguous_device",
                              message: "\(named.count) iPhones named \(name) are plugged in. Rename one, then try again.")
        }
    }
    if ios.count == 1 && (udid == nil || phones == 1) { return (ios[0], "only") }
    if ios.count > 1 || (ios.count == 1 && (phones ?? 0) > 1) {
        throw HelperError(code: "ambiguous_device",
                          message: "Mobster can't tell which plugged-in iPhone is this one. Unplug the others, then try again.")
    }
    return nil
}

// MARK: - The capture session

final class Screen: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let queue = DispatchQueue(label: "phone-screen.frames")
    let lock = NSLock()
    var latest: CVPixelBuffer?
    var latestAt = Date.distantPast
    var arrivals: [Date] = []
    var reference: [UInt8]?
    var opened = false
    let context = CIContext(options: [.useSoftwareRenderer: false])

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let now = Date()
        lock.lock()
        latest = buffer
        latestAt = now
        arrivals.append(now)
        if arrivals.count > 120 { arrivals.removeFirst(arrivals.count - 120) }
        lock.unlock()
    }

    func newest() throws -> (CVPixelBuffer, Date) {
        lock.lock()
        defer { lock.unlock() }
        guard opened else { throw HelperError(code: "not_open", message: "The iPhone's screen isn't open") }
        guard let buffer = latest else { throw HelperError(code: "capture", message: "The iPhone sent no picture yet") }
        return (buffer, latestAt)
    }

    func fps() -> Double {
        lock.lock()
        defer { lock.unlock() }
        let recent = arrivals.filter { Date().timeIntervalSince($0) < 2 }
        guard recent.count > 1, let first = recent.first, let last = recent.last, last > first else { return 0 }
        return Double(recent.count - 1) / last.timeIntervalSince(first)
    }

    func open(_ request: [String: Any]) throws -> [String: Any] {
        if opened { throw HelperError(code: "usage", message: "Already open") }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .denied, .restricted:
            throw HelperError(code: "permission", message: "macOS doesn't let Mobster see the iPhone's screen. Allow "
                              + "Camera for Mobster in System Settings > Privacy & Security, then try again.")
        default: break
        }
        let started = Date()
        allowScreenCaptureDevices()
        let udid = request["udid"] as? String
        let name = request["name"] as? String
        let phones = (request["phones"] as? NSNumber)?.intValue
        var found: (AVCaptureDevice, String)?
        let deadline = Date().addingTimeInterval((request["find_seconds"] as? NSNumber)?.doubleValue ?? 4)
        repeat {
            found = try pick(iphones(), udid: udid, name: name, phones: phones)
            if found != nil { break }
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        } while Date() < deadline
        guard let (device, match) = found else {
            throw HelperError(code: "no_device", message: "macOS shows no screen for this iPhone. Plug it in with a "
                              + "cable, unlock it and tap Trust if it asks, then try again.")
        }
        let findMs = ms(started)
        let openStarted = Date()
        let input = try AVCaptureDeviceInput(device: device)
        let output = AVCaptureVideoDataOutput()
        let width = (request["width"] as? NSNumber)?.intValue ?? 590
        // The phone's aspect: the device's own format when it says, else a 19.5:9 iPhone.
        var aspect = 2556.0 / 1179.0
        let dims = CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription)
        if dims.width > 0 && dims.height > 0 { aspect = Double(dims.height) / Double(dims.width) }
        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: width,
            kCVPixelBufferHeightKey as String: Int((Double(width) * aspect).rounded()),
        ]
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        session.beginConfiguration()
        guard session.canAddInput(input), session.canAddOutput(output) else {
            throw HelperError(code: "capture", message: "macOS wouldn't open the iPhone's screen")
        }
        session.addInput(input)
        session.addOutput(output)
        session.commitConfiguration()
        session.startRunning()
        lock.lock(); opened = true; lock.unlock()
        let openMs = ms(openStarted)
        let firstStarted = Date()
        let firstWait = (request["first_seconds"] as? NSNumber)?.doubleValue ?? 12
        while Date().timeIntervalSince(firstStarted) < firstWait {
            lock.lock(); let got = latest != nil; lock.unlock()
            if got { break }
            Thread.sleep(forTimeInterval: 0.01)
        }
        lock.lock(); let got = latest != nil; lock.unlock()
        if !got { throw HelperError(code: "capture", message: "The iPhone's screen sent no picture in \(Int(firstWait)) s. Unlock it, then try again.") }
        return ["device": ["id": device.uniqueID, "name": device.localizedName, "model": device.modelID],
                "match": match, "ms": ["find": findMs, "open": openMs, "first": ms(firstStarted)]]
    }

    func image(_ buffer: CVPixelBuffer) throws -> CGImage {
        let ci = CIImage(cvPixelBuffer: buffer)
        guard let image = context.createCGImage(ci, from: ci.extent) else {
            throw HelperError(code: "capture", message: "The frame couldn't be read")
        }
        return image
    }
}

// MARK: - Pixels

/// A small grey copy of the frame (cols x rows), luminance 0...255.
func grey(_ image: CGImage, cols: Int, rows: Int) -> [UInt8] {
    var pixels = [UInt8](repeating: 0, count: cols * rows)
    let context = CGContext(data: &pixels, width: cols, height: rows, bitsPerComponent: 8, bytesPerRow: cols,
                            space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)
    context?.interpolationQuality = .low
    context?.draw(image, in: CGRect(x: 0, y: 0, width: cols, height: rows))
    return pixels
}

/// The hash Mirroring's helper uses (same grid, same folding), and the share of near-black cells.
func signature(_ image: CGImage, mask: Double) -> (hash: String, dark: Double) {
    let pixels = grey(image, cols: 24, rows: 52)
    let skip = Int((Double(52) * mask).rounded(.up)) * 24
    var hash: UInt64 = 1469598103934665603
    var dark = 0
    for (index, value) in pixels.enumerated() where index >= skip {
        hash = (hash ^ UInt64(value >> 3)) &* 1099511628211
        if value < 10 { dark += 1 }
    }
    return (String(format: "%016llx", hash), Double(dark) / Double(max(pixels.count - skip, 1)))
}

func masked(_ image: CGImage, _ fraction: Double) -> CGImage {
    guard fraction > 0,
          let context = CGContext(data: nil, width: image.width, height: image.height, bitsPerComponent: 8,
                                  bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return image }
    let full = CGRect(x: 0, y: 0, width: image.width, height: image.height)
    context.draw(image, in: full)
    let band = Double(image.height) * fraction
    context.setFillColor(CGColor(gray: 0, alpha: 1))
    context.fill(CGRect(x: 0, y: Double(image.height) - band, width: Double(image.width), height: band))
    return context.makeImage() ?? image
}

func writeJPEG(_ image: CGImage, _ path: String) throws {
    let url = URL(fileURLWithPath: path)
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil)
    else { throw HelperError(code: "capture", message: "Couldn't write the frame") }
    CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: 0.8] as CFDictionary)
    if !CGImageDestinationFinalize(destination) {
        throw HelperError(code: "capture", message: "Couldn't write the frame")
    }
}

func recognize(_ image: CGImage, accurate: Bool) throws -> [[String: Any]] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = accurate ? .accurate : .fast
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    return (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return ["text": candidate.string, "confidence": Double(candidate.confidence),
                "rect": [Double(box.minX), Double(1 - box.maxY), Double(box.width), Double(box.height)]]
    }
}

let BLOB_COLS = 148
let BLOB_ROWS = 320

/// Regions that differ between two grey frames: 4-connected cells over the threshold, largest first.
func blobs(_ a: [UInt8], _ b: [UInt8], threshold: Int, minimum: Int) -> [[String: Any]] {
    let cols = BLOB_COLS, rows = BLOB_ROWS
    var hot = [Bool](repeating: false, count: cols * rows)
    for i in 0..<min(a.count, b.count) where abs(Int(a[i]) - Int(b[i])) >= threshold { hot[i] = true }
    var seen = [Bool](repeating: false, count: cols * rows)
    var found: [[String: Any]] = []
    for start in 0..<hot.count where hot[start] && !seen[start] {
        var stack = [start]
        seen[start] = true
        var count = 0, minX = cols, minY = rows, maxX = 0, maxY = 0, sumX = 0, sumY = 0
        while let cell = stack.popLast() {
            let x = cell % cols, y = cell / cols
            count += 1; sumX += x; sumY += y
            minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
            for (nx, ny) in [(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)]
            where nx >= 0 && ny >= 0 && nx < cols && ny < rows {
                let n = ny * cols + nx
                if hot[n] && !seen[n] { seen[n] = true; stack.append(n) }
            }
        }
        if count >= minimum {
            found.append(["x": (Double(sumX) / Double(count) + 0.5) / Double(cols),
                          "y": (Double(sumY) / Double(count) + 0.5) / Double(rows),
                          "w": Double(maxX - minX + 1) / Double(cols), "h": Double(maxY - minY + 1) / Double(rows),
                          "area": count])
        }
    }
    found.sort { ($0["area"] as? Int ?? 0) > ($1["area"] as? Int ?? 0) }
    return Array(found.prefix(12))
}

// MARK: - Requests

let screen = Screen()

func number(_ request: [String: Any], _ key: String, _ fallback: Double) -> Double {
    (request[key] as? NSNumber)?.doubleValue ?? fallback
}

func handle(_ request: [String: Any]) throws -> [String: Any] {
    switch request["op"] as? String ?? "" {
    case "open":
        return try screen.open(request)
    case "capture":
        let started = Date()
        let (buffer, at) = try screen.newest()
        let mask = max(0, min(number(request, "mask", 0), 0.2))
        let image = masked(try screen.image(buffer), mask)
        let captured = Date()
        let sig = signature(image, mask: mask)
        var out: [String: Any] = ["hash": sig.hash, "dark": sig.dark, "pixels": [image.width, image.height],
                                  "window": ["w": image.width, "h": image.height], "age": ms(at), "fps": screen.fps()]
        if let path = request["path"] as? String { try writeJPEG(image, path) }
        if request["ocr"] as? Bool == true {
            out["rows"] = try recognize(image, accurate: request["fast"] as? Bool != true).filter { row in
                guard let rect = row["rect"] as? [Double], rect.count == 4 else { return false }
                return rect[1] + rect[3] / 2 >= mask
            }
        }
        out["ms"] = ["capture": Int(captured.timeIntervalSince(started) * 1000), "total": ms(started)]
        return out
    case "frame":
        // The live view's picture: scaled on the GPU to `width`, as an inline JPEG, or {"same":true} when it is the
        // picture keyed `after` (a still screen costs one small comparison).
        let (buffer, at) = try screen.newest()
        let full = CIImage(cvPixelBuffer: buffer)
        let width = max(64, min(number(request, "width", 590), Double(full.extent.width)))
        let scale = width / Double(full.extent.width)
        let small = full.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        guard let image = screen.context.createCGImage(small, from: small.extent.integral) else {
            throw HelperError(code: "capture", message: "The frame couldn't be read")
        }
        let key = signature(image, mask: 0).hash + "/\(image.width)"
        if let after = request["after"] as? String, after == key {
            return ["same": true, "key": key, "age": ms(at)]
        }
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil)
        else { throw HelperError(code: "capture", message: "Couldn't encode the frame") }
        CGImageDestinationAddImage(destination, image,
                                   [kCGImageDestinationLossyCompressionQuality: number(request, "quality", 0.5)] as CFDictionary)
        CGImageDestinationFinalize(destination)
        return ["key": key, "jpeg": (data as Data).base64EncodedString(), "w": image.width, "h": image.height,
                "age": ms(at)]
    case "mark":
        let (buffer, _) = try screen.newest()
        screen.reference = grey(try screen.image(buffer), cols: BLOB_COLS, rows: BLOB_ROWS)
        return [:]
    case "blobs":
        guard let reference = screen.reference else { throw HelperError(code: "usage", message: "Nothing marked") }
        let (buffer, at) = try screen.newest()
        let now = grey(try screen.image(buffer), cols: BLOB_COLS, rows: BLOB_ROWS)
        return ["blobs": blobs(reference, now, threshold: Int(number(request, "threshold", 28)),
                               minimum: Int(number(request, "min", 4))), "age": ms(at)]
    default:
        throw HelperError(code: "usage", message: "Unknown op")
    }
}

// Requests run side by side: the live view's frames never wait behind a 250 ms text read. One driver sends one
// request at a time, so mark and blobs stay in order.
let output = NSLock()
let group = DispatchGroup()
func reply(_ value: [String: Any]) {
    output.lock()
    emit(value)
    output.unlock()
}
while let line = readLine() {
    guard let data = line.data(using: .utf8),
          let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
        reply(["ok": false, "code": "usage", "error": "Not a JSON object"])
        continue
    }
    let id = request["id"] ?? NSNull()
    let work = { () -> Void in
        do {
            var answer = try handle(request)
            answer["id"] = id
            answer["ok"] = true
            reply(answer)
        } catch let error as HelperError {
            reply(["id": id, "ok": false, "code": error.code, "error": error.message])
        } catch {
            reply(["id": id, "ok": false, "code": "capture", "error": "\(error)"])
        }
    }
    // Opening runs on this thread (AVFoundation finds devices on the run loop); everything else beside it.
    if request["op"] as? String == "open" { work() } else { DispatchQueue.global(qos: .userInitiated).async(group: group, execute: work) }
}
group.wait()
screen.session.stopRunning()
