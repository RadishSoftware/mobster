// doc-text: reads the text of a PDF or an image on this Mac, for files attached to a Mobster task.
//
//   doc-text pdf IN TEXT_OUT [--thumb JPG_OUT --size PX] [--ocr-empty PAGES]
//       Each page's text (PDFKit) into TEXT_OUT, pages separated by a form feed (\f). With --thumb, the first page
//       as a JPEG at most PX on its longer side. With --ocr-empty, up to PAGES pages that have no text layer (a
//       scan) are read with Vision instead.
//   doc-text ocr IN TEXT_OUT
//       The text in an image (PNG, JPEG, HEIC, GIF, WebP), read with Vision, one line per line it finds.
//
// Prints one JSON line: {"ok":true,"pages":n,"chars":n,"thumb":bool,"ocrPages":n} or {"ok":false,"error":"..."}
// ("locked": the PDF needs a password; "unreadable": not a PDF or image this Mac can open). Exits 0 either way;
// 2 for a usage error. Everything happens on this Mac: Vision's text recognition runs on device.
//
// The Mac app carries it as Contents/Resources/doc-text; a source checkout builds it with native_helpers.build("doc_text").

import CoreGraphics
import Foundation
import ImageIO
import PDFKit
import UniformTypeIdentifiers
import Vision

let maxPages = 2000

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
       let line = String(data: data, encoding: .utf8) {
        print(line)
    }
}

func fail(_ error: String) -> Never {
    emit(["ok": false, "error": error])
    exit(0)
}

func usage() -> Never {
    FileHandle.standardError.write("usage: doc-text pdf IN TEXT_OUT [--thumb JPG_OUT --size PX] [--ocr-empty PAGES] | doc-text ocr IN TEXT_OUT\n".data(using: .utf8)!)
    exit(2)
}

func option(_ args: [String], _ name: String) -> String? {
    guard let index = args.firstIndex(of: name), index + 1 < args.count else { return nil }
    return args[index + 1]
}

func writeJPEG(_ image: CGImage, to path: String, quality: Double = 0.82) -> Bool {
    let url = URL(fileURLWithPath: path)
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else {
        return false
    }
    CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
    return CGImageDestinationFinalize(destination)
}

/// The page drawn on white, scaled so its longer side is `longest` pixels.
func render(_ page: PDFPage, longest: CGFloat) -> CGImage? {
    let box = page.bounds(for: .mediaBox)
    guard box.width > 0, box.height > 0 else { return nil }
    let rotated = page.rotation % 180 != 0
    let width = rotated ? box.height : box.width
    let height = rotated ? box.width : box.height
    let scale = min(longest / max(width, height), 4)
    let pixelsWide = max(1, Int((width * scale).rounded()))
    let pixelsHigh = max(1, Int((height * scale).rounded()))
    guard let context = CGContext(data: nil, width: pixelsWide, height: pixelsHigh, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
    context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: pixelsWide, height: pixelsHigh))
    context.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: context)
    return context.makeImage()
}

func recognize(_ image: CGImage) -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return ""
    }
    let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    return lines.joined(separator: "\n")
}

func pdf(_ args: [String]) -> Never {
    guard args.count >= 4 else { usage() }
    let input = URL(fileURLWithPath: args[2])
    let output = args[3]
    guard let document = PDFDocument(url: input) else { fail("unreadable") }
    if document.isLocked && !document.unlock(withPassword: "") { fail("locked") }
    let count = min(document.pageCount, maxPages)
    var ocrBudget = Int(option(args, "--ocr-empty") ?? "0") ?? 0
    var pages: [String] = []
    var ocrPages = 0
    for index in 0..<count {
        guard let page = document.page(at: index) else { pages.append(""); continue }
        var text = (page.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty && ocrBudget > 0, let image = render(page, longest: 2000) {
            ocrBudget -= 1
            text = recognize(image)
            if !text.isEmpty { ocrPages += 1 }
        }
        pages.append(text)
    }
    let joined = pages.joined(separator: "\u{0C}")
    do {
        try joined.write(toFile: output, atomically: true, encoding: .utf8)
    } catch {
        fail("write")
    }
    var thumb = false
    if let thumbPath = option(args, "--thumb"), let first = document.page(at: 0) {
        let size = CGFloat(Double(option(args, "--size") ?? "480") ?? 480)
        if let image = render(first, longest: size) { thumb = writeJPEG(image, to: thumbPath) }
    }
    emit(["ok": true, "pages": document.pageCount, "chars": joined.count, "thumb": thumb, "ocrPages": ocrPages])
    exit(0)
}

func ocr(_ args: [String]) -> Never {
    guard args.count >= 4 else { usage() }
    let input = URL(fileURLWithPath: args[2])
    guard let source = CGImageSourceCreateWithURL(input as CFURL, nil) else { fail("unreadable") }
    let options = [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceThumbnailMaxPixelSize: 4096,
                   kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary
    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options) else { fail("unreadable") }
    let text = recognize(image)
    do {
        try text.write(toFile: args[3], atomically: true, encoding: .utf8)
    } catch {
        fail("write")
    }
    emit(["ok": true, "pages": 1, "chars": text.count, "thumb": false, "ocrPages": text.isEmpty ? 0 : 1])
    exit(0)
}

@main
struct DocText {
    static func main() {
        let args = CommandLine.arguments
        guard args.count >= 2 else { usage() }
        switch args[1] {
        case "pdf": pdf(args)
        case "ocr": ocr(args)
        default: usage()
        }
    }
}
