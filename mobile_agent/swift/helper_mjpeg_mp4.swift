// mjpeg-mp4: encodes an iPhone's screen, as JPEG frames on stdin, into an H.264 MP4 (AVFoundation, no ffmpeg).
//   mjpeg-mp4 <out.mp4> [--width W]   W: the video's width in pixels (default 590, half an iPhone's), even
// stdin carries one record per frame: the frame's time in ms since the recording began (8 bytes, big-endian), the
// JPEG's length (4 bytes, big-endian), then the JPEG. Each frame shows until the next one's time, so a still screen
// costs one frame. End of input finishes the file. Exit 0 with the MP4 written; 1 with one line on stderr otherwise.
import AVFoundation
import CoreGraphics
import Foundation
import ImageIO

let args = CommandLine.arguments
func fail(_ message: String) -> Never { FileHandle.standardError.write((message + "\n").data(using: .utf8)!); exit(1) }
guard args.count >= 2 else { fail("usage: mjpeg-mp4 out.mp4 [--width W]") }
let output = URL(fileURLWithPath: args[1])
var maxWidth = 590
if let i = args.firstIndex(of: "--width"), i + 1 < args.count, let w = Int(args[i + 1]), w >= 64 { maxWidth = w }

let input = FileHandle.standardInput
func read(_ count: Int) -> Data? {
  var data = Data()
  while data.count < count {
    let chunk = input.readData(ofLength: count - data.count)
    if chunk.isEmpty { return nil }
    data.append(chunk)
  }
  return data
}
func number(_ data: Data) -> UInt64 { data.reduce(0) { ($0 << 8) | UInt64($1) } }

func decode(_ jpeg: Data) -> CGImage? {
  guard let source = CGImageSourceCreateWithData(jpeg as CFData, nil) else { return nil }
  return CGImageSourceCreateImageAtIndex(source, 0, nil)
}

var writer: AVAssetWriter?
var writerInput: AVAssetWriterInput?
var adaptor: AVAssetWriterInputPixelBufferAdaptor?
var size = CGSize.zero
var written = 0

func start(_ image: CGImage) {
  let width = min(maxWidth, image.width) & ~1
  let height = max(2, Int((Double(image.height) * Double(width) / Double(image.width)).rounded()) & ~1)
  size = CGSize(width: width, height: height)
  try? FileManager.default.removeItem(at: output)
  guard let made = try? AVAssetWriter(outputURL: output, fileType: .mp4) else { fail("can't write \(output.path)") }
  let settings: [String: Any] = [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height,
                                 AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 1_200_000,
                                                                   AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel]]
  let videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
  videoInput.expectsMediaDataInRealTime = true
  let attributes: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                                   kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height]
  let made2 = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: videoInput, sourcePixelBufferAttributes: attributes)
  guard made.canAdd(videoInput) else { fail("can't add the video track") }
  made.add(videoInput)
  guard made.startWriting() else { fail("can't start writing: \(made.error?.localizedDescription ?? "unknown")") }
  made.startSession(atSourceTime: .zero)
  writer = made; writerInput = videoInput; adaptor = made2
}

func append(_ image: CGImage, at ms: UInt64) {
  guard let adaptor = adaptor, let videoInput = writerInput, let pool = adaptor.pixelBufferPool else { return }
  var buffer: CVPixelBuffer?
  guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer) == kCVReturnSuccess, let pixels = buffer else { return }
  CVPixelBufferLockBaseAddress(pixels, [])
  if let context = CGContext(data: CVPixelBufferGetBaseAddress(pixels), width: Int(size.width), height: Int(size.height),
                             bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixels),
                             space: CGColorSpaceCreateDeviceRGB(),
                             bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue) {
    context.setFillColor(CGColor(gray: 0, alpha: 1))
    context.fill(CGRect(origin: .zero, size: size))
    // A frame of another shape (the phone turned) is fitted inside, centred.
    let scale = min(size.width / CGFloat(image.width), size.height / CGFloat(image.height))
    let drawn = CGSize(width: CGFloat(image.width) * scale, height: CGFloat(image.height) * scale)
    context.interpolationQuality = .high
    context.draw(image, in: CGRect(x: (size.width - drawn.width) / 2, y: (size.height - drawn.height) / 2,
                                   width: drawn.width, height: drawn.height))
  }
  CVPixelBufferUnlockBaseAddress(pixels, [])
  var waited = 0
  while !videoInput.isReadyForMoreMediaData && waited < 2000 { usleep(1000); waited += 1 }
  if adaptor.append(pixels, withPresentationTime: CMTime(value: CMTimeValue(ms), timescale: 1000)) { written += 1 }
}

var last: UInt64 = 0
while let head = read(12) {
  let ms = number(head.prefix(8)), length = Int(number(head.suffix(4)))
  guard length > 0, length < 16 * 1024 * 1024, let jpeg = read(length) else { break }
  guard let image = decode(jpeg) else { continue }
  if writer == nil { start(image) }
  if written > 0 && ms <= last { continue }  // presentation times must rise
  append(image, at: ms)
  last = ms
}

guard let made = writer, let videoInput = writerInput, written > 0 else { fail("no frames") }
videoInput.markAsFinished()
made.endSession(atSourceTime: CMTime(value: CMTimeValue(last), timescale: 1000))
let done = DispatchSemaphore(value: 0)
made.finishWriting { done.signal() }
done.wait()
if made.status != .completed { fail("the MP4 didn't finish: \(made.error?.localizedDescription ?? "unknown")") }
