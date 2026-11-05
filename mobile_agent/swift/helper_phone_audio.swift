// phone-audio: hears what a USB iPhone plays, on this Mac, for Mobster's WATCH and MCP's listen (mobile_agent/listen.py).
//
//   phone-audio devices
//       The iPhones this Mac can hear now: {"ok":true,"devices":[{"id","name","model"}]}.
//   phone-audio listen --seconds N [--udid UDID [--name NAME] [--phones K]] [--locale en-US] [--work DIR]
//                      [--keep PATH] [--live [--frames]]
//       Records N seconds (1 to 120) of the iPhone's audio, then turns its speech into text on this Mac:
//       {"ok":true,"text","segments","engine","locale","seconds","recordedSeconds","findMs","startMs","recordMs",
//       "transcribeMs","peakDb","match","soundWindows","classifyMs"}. The iPhone is the capture device whose uniqueID
//       is UDID, else the one named NAME (the phone's name as the device layer reads it), else the only iOS capture
//       device when K, the USB iPhones the device layer sees, is 1; with no UDID, the only one. match says which: udid,
//       name or only. segments are [{"start","end","text"}], in seconds from the recording's start; soundWindows are
//       the sounds told apart in it (see below).
//       The recording is a private file in DIR (else the temporary folder), deleted before this exits, unless --keep
//       names a file to keep it in ("kept").
//       With --live (WATCH, mobile_agent/watch.py): {"event":"recording"} once the recording has started, then
//       {"event":"level","t","db"} every half second (the audio's average power), and the recording ends early when
//       stdin reads a line "stop" or closes. With --frames too, the phone's picture from the same capture:
//       {"event":"frame","seq","path","t"} about 4 times a second, a 590-pixel-wide JPEG in the private folder (only
//       the newest 8 kept), so WATCH needn't read the screen through WebDriverAgent while the capture runs (opening
//       it resets the phone's USB network link, which ends WebDriverAgent's runner). The answer is still the last
//       line. Before the first capture macOS asks whether Mobster may use the camera and the microphone (see
//       authorize); while it asks, {"event":"asking","service"} comes first.
//   phone-audio record-video --seconds N --udid UDID [--name NAME] [--phones K] --work DIR
//       Records 1 to 300 seconds of the iPhone's screen and the audio it offers into DIR/video.mov, picking the
//       capture device as listen does: {"ok":true,"seconds","durationSeconds","audio","bytes","match"}. DIR must be a
//       private folder (mode 0700, yours); the caller deletes it, even if this process is killed. A capture device
//       another app is using is never taken over: capture_busy.
//   phone-audio match [--udid UDID] [--name NAME] [--phones K] < [{"id","name","model"}]
//       Which device listen would pick from that list: {"ok":true,"index","match"}, or listen's failure. For tests and
//       diagnosis; nothing is captured.
//   phone-audio transcribe FILE [--locale en-US]
//       The speech in an audio file, the same way: {"ok":true,"text","segments","engine","locale","transcribeMs"}.
//   phone-audio classify FILE
//       The sounds in an audio file, the way listen classifies them: {"ok":true,"soundWindows","classifyMs"}. For tests
//       and diagnosis; nothing is captured.
//   phone-audio status [--locale en-US]
//       What this Mac can understand, without asking for anything: {"ok":true,"speechAnalyzer","locale","sfSpeech":
//       {"authorization","onDevice"},"capture":{"camera","microphone"}}. capture is granted, denied, restricted or
//       not_determined for each. speechAnalyzer is installed, supported (its model isn't downloaded),
//       downloading, unsupported (not this language) or unavailable (before macOS 26).
//   phone-audio prepare [--locale en-US]
//       Installs macOS's own on-device speech model for the language (SpeechAnalyzer's, the one Voice uses), once:
//       {"ok":true,"speechAnalyzer":"installed","locale"}. Nothing else is downloaded, ever.
//
// Failures are {"ok":false,"error","message"}: no_device (no USB iPhone to hear), ambiguous_device (several iPhones,
// and none is surely the one asked for: it never guesses), permission (macOS doesn't let Mobster capture the iPhone:
// denied, refused without asking, or its prompt unanswered for ASK_SECONDS), capture_failed, no_engine (no on-device speech recognition for that language), speech_denied,
// transcribe_failed. Exit 0 either way; 2 for a usage error.
//
// How it hears: CoreMediaIO's kCMIOHardwarePropertyAllowScreenCaptureDevices, turned on in this process only, makes a
// USB iPhone appear as a capture device: the one QuickTime Player records from. AVCaptureAudioFileOutput records its
// audio track as 16-bit mono WAV. Nothing is tapped on the phone, and nothing is captured outside the N seconds.
//
// How it understands: SpeechAnalyzer with SpeechTranscriber on macOS 26 and later, else SFSpeechRecognizer with
// requiresOnDeviceRecognition. Both run on this Mac. SpeechAnalyzer downloads Apple's speech model once when it is
// missing; if installation fails, on-device SFSpeechRecognizer is tried. There is no server recognition fallback:
// without an available on-device model, it answers no_engine. What it hears is data: it goes to stdout as text.
//
// How it tells sounds apart: SoundAnalysis's built-in classifier (SNClassifySoundRequest, 303 labels: music and its
// instruments, laughter, applause, a dog, a siren), on this Mac, alongside the transcription and on the same
// recording. soundWindows are [{"t","d","labels":{label: confidence}}]: a 3-second window every 1.5 s (the whole
// file when it is shorter), each with its labels at least SOUND_FLOOR sure, at most SOUND_LABELS of them.
// mobile_agent/listen.py collapses them into timed sounds. A classifier failure leaves soundWindows out and says why
// in soundsError; the words still stand.

import AVFoundation
import CoreImage
import CoreMediaIO
import Foundation
import SoundAnalysis
import Speech

// MARK: - Output

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}

/// Folders to delete before exiting, whichever way it exits: the recording never outlives this process unasked.
var cleanup: [URL] = []

func removeTemporary() {
    for url in cleanup { try? FileManager.default.removeItem(at: url) }
    cleanup = []
}

func fail(_ error: String, _ message: String) -> Never {
    removeTemporary()
    emit(["ok": false, "error": error, "message": message])
    exit(0)
}

/// Turns the main run loop until `done()` or `timeout` seconds: AVFoundation and Speech deliver here.
@discardableResult
func pump(timeout: TimeInterval, until done: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while !done() {
        if Date() >= deadline { return false }
        RunLoop.current.run(until: Date().addingTimeInterval(0.02))
    }
    return true
}

func usage() -> Never {
    FileHandle.standardError.write(Data(("usage: phone-audio devices | phone-audio listen --seconds N [--udid UDID] [--name NAME] "
        + "[--phones K] [--locale L] [--work DIR] [--keep PATH] [--live [--frames]] | phone-audio match [--udid UDID] "
        + "[--name NAME] [--phones K] < devices.json | phone-audio transcribe FILE [--locale L] "
        + "| phone-audio classify FILE | phone-audio record-video --seconds N --udid UDID [--name NAME] "
        + "[--phones K] --work DIR\n").utf8))
    exit(2)
}

func milliseconds(since start: Date) -> Int { Int((Date().timeIntervalSince(start) * 1000).rounded()) }

// MARK: - Arguments

var arguments = Array(CommandLine.arguments.dropFirst())
guard let command = arguments.first else { usage() }
arguments.removeFirst()

func option(_ name: String) -> String? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
    return arguments[index + 1]
}

// MARK: - The iPhone as a capture device

/// Lets this process see screen-capture devices (USB iPhones). Only this process: QuickTime's own setting is untouched.
func allowScreenCaptureDevices() {
    var address = CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
    var allow: UInt32 = 1
    CMIOObjectSetPropertyData(CMIOObjectID(kCMIOObjectSystemObject), &address, 0, nil,
                              UInt32(MemoryLayout<UInt32>.size), &allow)
}

/// The iPhones this Mac can capture: external muxed devices made by Apple.
func iphones() -> [AVCaptureDevice] {
    let types: [AVCaptureDevice.DeviceType]
    if #available(macOS 14.0, *) { types = [.external] } else { types = [.externalUnknown] }
    return AVCaptureDevice.DiscoverySession(deviceTypes: types, mediaType: .muxed, position: .unspecified).devices
}

/// A UDID as the capture device's uniqueID may spell it: no dashes, upper case.
func plain(_ id: String) -> String { id.replacingOccurrences(of: "-", with: "").uppercased() }

/// A name compared the way people read it: its words, any case.
func folded(_ name: String) -> String {
    name.split(whereSeparator: \.isWhitespace).joined(separator: " ").folding(options: [.caseInsensitive,
                                                                                         .diacriticInsensitive],
                                                                                locale: nil)
}

/// What the matching reads of a capture device: AVCaptureDevice's uniqueID, localizedName and modelID.
struct Candidate { let id: String; let name: String; let model: String }

/// Which iPhone to hear, and how it was told: "udid", "name" or "only" (the only iOS capture device, on the only
/// USB iPhone). ambiguous(n): n iOS capture devices and none is the one asked for. none: no iOS capture device fits.
enum Pick: Equatable { case found(Int, String), ambiguous(Int), none }

/// An iPhone or iPad as a capture device: AVFoundation's modelID for one is "iOS Device".
func looksLikeIOS(_ device: Candidate) -> Bool {
    let model = device.model.lowercased()
    return model.contains("ios") || model.hasPrefix("iphone") || model.hasPrefix("ipad")
}

/// The capture device for the iPhone asked for. On some Macs an iPhone's capture device has a uniqueID of its own,
/// not its UDID (measured 13 Oct 2026), so after the UDID it tries the phone's name as the device layer reads it
/// (`name`, passed only when no other USB iPhone shares it), then the only iOS capture device, when the device layer
/// sees one USB iPhone (`phones`) or no UDID was asked for. Never a guess between two phones.
func pick(_ found: [Candidate], udid: String?, name: String?, phones: Int?) -> Pick {
    if let udid = udid, let index = found.firstIndex(where: { plain($0.id) == plain(udid) }) {
        return .found(index, "udid")
    }
    let ios = found.indices.filter { looksLikeIOS(found[$0]) }
    if let name = name, !folded(name).isEmpty {
        let named = ios.filter { folded(found[$0].name) == folded(name) }
        if named.count == 1 { return .found(named[0], "name") }
        if named.count > 1 { return .ambiguous(named.count) }
    }
    if ios.count == 1 && (udid == nil || phones == 1) { return .found(ios[0], "only") }
    if ios.count > 1 || (ios.count == 1 && (phones ?? 0) > 1) { return .ambiguous(max(ios.count, phones ?? 0)) }
    return .none
}

func candidates(_ devices: [AVCaptureDevice]) -> [Candidate] {
    devices.map { Candidate(id: $0.uniqueID, name: $0.localizedName, model: $0.modelID) }
}

/// The iPhone to hear (see pick). They appear a moment after the property is set, so look for up to `timeout`
/// seconds; the answer then is the last look's.
func findPhone(udid: String?, name: String?, phones: Int?, timeout: TimeInterval) -> (AVCaptureDevice?, Pick) {
    let deadline = Date().addingTimeInterval(timeout)
    var last = Pick.none
    repeat {
        let found = iphones()
        last = pick(candidates(found), udid: udid, name: name, phones: phones)
        if case .found(let index, _) = last { return (found[index], last) }
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    } while Date() < deadline
    return (nil, last)
}

/// Why no iPhone was picked, as listen's failure: what happened, then what to do. It names the phone asked for.
func refuse(_ result: Pick, name: String?) -> Never {
    let phone = name.map { "\u{201C}\($0)\u{201D}" }
    if case .ambiguous(let count) = result {
        fail("ambiguous_device", phone.map {
            "\(count) iPhones are plugged in, and Mobster can't tell which one is \($0). Unplug the others, then try again."
        } ?? "\(count) iPhones are plugged in, and Mobster can't tell which one to hear. Unplug the others, then try again.")
    }
    fail("no_device", phone.map {
        "No iPhone to hear: macOS shows no capture device for \($0). Plug it in with a cable, unlock it, then try again."
    } ?? "No iPhone to hear: plug one in with a cable and unlock it.")
}

// MARK: - Permission

/// What macOS says when Mobster may not capture the iPhone: listen's failure "permission".
let PERMISSION_MESSAGE = "Mobster needs permission to hear your iPhone. Allow it in System Settings › Privacy & Security › Microphone/Camera."
/// How long the person has to answer macOS's prompt before listen gives up (the prompt stays up; the next try hears).
let ASK_SECONDS = 60.0

func authorizationName(_ status: AVAuthorizationStatus) -> String {
    switch status {
    case .authorized: return "granted"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not_determined"
    @unknown default: return "unknown"
    }
}

/// Makes sure macOS lets this process capture the iPhone, before anything is opened. macOS counts the iPhone's
/// capture device as a camera with a microphone and asks for both, attributed to the app that started this helper
/// (Mobster.app, which must hold the camera and audio-input entitlements: tccd checks the responsible app, not this
/// helper). Granted: returns. Denied, or refused without a prompt (tccd's policy: the status then stays
/// not_determined): fails "permission". Not asked yet: asks, and with `live` says {"event":"asking","service"} first,
/// so WATCH can wait for the answer; `stop` (WATCH gave up waiting) ends the wait as an unanswered prompt.
func authorize(live: Bool, stop: StopSignal? = nil) {
    for (media, service) in [(AVMediaType.video, "camera"), (AVMediaType.audio, "microphone")] {
        switch AVCaptureDevice.authorizationStatus(for: media) {
        case .authorized:
            continue
        case .notDetermined:
            if live { emit(["event": "asking", "service": service]) }
            let answer = SendableBox<Bool>()
            AVCaptureDevice.requestAccess(for: media) { answer.value = $0 }
            pump(timeout: ASK_SECONDS) { answer.value != nil || stop?.isSet == true }
            guard answer.value != nil else {
                fail("permission", "macOS is asking whether Mobster may hear your iPhone. Answer its prompt, then try again.")
            }
            if answer.value != true { fail("permission", PERMISSION_MESSAGE) }
        default:
            fail("permission", PERMISSION_MESSAGE)
        }
    }
}

// MARK: - Recording

final class Recorder: NSObject, AVCaptureFileOutputRecordingDelegate {
    private let lock = NSLock()
    private var finished = false
    private var started = false
    private(set) var error: Error?
    var done: Bool { lock.lock(); defer { lock.unlock() }; return finished }
    var recording: Bool { lock.lock(); defer { lock.unlock() }; return started }

    func fileOutput(_ output: AVCaptureFileOutput, didStartRecordingTo fileURL: URL, from connections: [AVCaptureConnection]) {
        lock.lock()
        started = true
        lock.unlock()
    }

    func fileOutput(_ output: AVCaptureFileOutput, didFinishRecordingTo outputFileURL: URL,
                    from connections: [AVCaptureConnection], error: Error?) {
        // Reaching the maximum duration is how a recording ends here, and AVFoundation reports it as an error.
        lock.lock()
        if let error = error as NSError?, !(error.userInfo[AVErrorRecordingSuccessfullyFinishedKey] as? Bool ?? false) {
            self.error = error
        }
        finished = true
        lock.unlock()
    }
}

/// Records the selected USB phone's screen, with its audio when it offers one, into `url`. A capture device that
/// another app is using is never taken over. Returns whether the movie has an audio track.
func recordVideo(_ device: AVCaptureDevice, seconds: Double, to url: URL) -> Bool {
    guard !device.isInUseByAnotherApplication, !device.isSuspended else {
        fail("capture_busy", "The iPhone's capture device is busy or suspended. Close its other capture first.")
    }
    let session = AVCaptureSession()
    let input: AVCaptureDeviceInput
    do { input = try AVCaptureDeviceInput(device: device) } catch {
        if (error as NSError).code == AVError.applicationIsNotAuthorizedToUseDevice.rawValue {
            fail("permission", PERMISSION_MESSAGE)
        }
        fail("capture_failed", "Mobster couldn't open the iPhone's video (\(error.localizedDescription)).")
    }
    guard session.canAddInput(input) else { fail("capture_failed", "The iPhone offered no capture input.") }
    session.addInput(input)
    let output = AVCaptureMovieFileOutput()
    output.maxRecordedDuration = CMTime(seconds: seconds, preferredTimescale: 600)
    guard session.canAddOutput(output) else { fail("capture_failed", "The iPhone offered no movie output.") }
    session.addOutput(output)
    guard output.connection(with: .video) != nil else {
        fail("capture_failed", "The iPhone offered no video to record. Unlock it and check the USB cable.")
    }
    let audio = output.connection(with: .audio) != nil
    let cancelled = SendableBox<Bool>()
    // The signal sources run on the main queue, outside the signal handler. The movie is always finished first.
    let signals = [SIGINT, SIGTERM].map { number -> DispatchSourceSignal in
        signal(number, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
        source.setEventHandler { cancelled.value = true }
        source.resume()
        return source
    }
    defer { for source in signals { source.cancel() } }
    session.startRunning()
    let recorder = Recorder()
    output.startRecording(to: url, recordingDelegate: recorder)
    let completed = pump(timeout: seconds + 10) { recorder.done || cancelled.value == true }
    if !recorder.done {
        output.stopRecording()
        pump(timeout: 5) { recorder.done }
    }
    session.stopRunning()
    if cancelled.value == true { fail("cancelled", "The recording was cancelled.") }
    guard completed, recorder.done else { fail("capture_failed", "The iPhone's video capture didn't finish in time.") }
    if let error = recorder.error {
        fail("capture_failed", "The video recording stopped early (\(error.localizedDescription)).")
    }
    return audio
}

/// Set once stdin reads "stop" or closes (--live): the recording then ends early, and what it has is kept.
final class StopSignal: @unchecked Sendable {
    private let lock = NSLock()
    private var stopped = false
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return stopped }
    func set() { lock.lock(); stopped = true; lock.unlock() }
}

func watchStdin(_ signal: StopSignal) {
    Thread.detachNewThread {
        while let line = readLine() {
            if line.trimmingCharacters(in: .whitespaces) == "stop" { break }
        }
        signal.set()
    }
}

/// The phone's picture from the same capture (--frames DIR, WATCH): while it runs, WebDriverAgent can't be relied on
/// (opening the capture resets the phone's USB network link, which ends its runner), so WATCH reads the screen here.
/// FRAME_HZ JPEGs a second, FRAME_WIDTH pixels wide, as DIR/frame-<seq>.jpg; only the newest FRAMES_KEPT stay.
let FRAME_HZ = 4.0
let FRAME_WIDTH = 590.0
let FRAMES_KEPT = 8

final class FrameWriter: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private let folder: URL
    private let context = CIContext()
    private var last = 0.0
    private var written: [URL] = []
    private(set) var seq = 0
    private var newest: (Int, String)?
    init(folder: URL) { self.folder = folder }

    /// The newest frame written since the last call, as (seq, path), else nil.
    func take() -> (Int, String)? { lock.lock(); defer { lock.unlock() }; let value = newest; newest = nil; return value }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        let now = Date().timeIntervalSinceReferenceDate
        guard now - last >= 1 / FRAME_HZ, let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        last = now
        var image = CIImage(cvPixelBuffer: pixels)
        let width = image.extent.width
        if width > FRAME_WIDTH { image = image.transformed(by: CGAffineTransform(scaleX: FRAME_WIDTH / width, y: FRAME_WIDTH / width)) }
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let data = context.jpegRepresentation(of: image, colorSpace: space,
                                                    options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.7])
        else { return }
        lock.lock()
        seq += 1
        let url = folder.appendingPathComponent("frame-\(seq).jpg")
        lock.unlock()
        guard (try? data.write(to: url, options: .atomic)) != nil else { return }
        lock.lock()
        written.append(url)
        let old = written.count > FRAMES_KEPT ? written.removeFirst() : nil
        newest = (seq, url.path)
        lock.unlock()
        if let old = old { try? FileManager.default.removeItem(at: old) }
    }
}

/// The sound through a data output, for --frames: AVCaptureAudioFileOutput refuses to record ("Cannot Record") once a
/// video output shares the session (measured on the test device, 14 Oct 2026), so the samples are written to the same WAV
/// (16-bit mono at 16 kHz) here. It stops taking samples at `limit` seconds, and keeps the newest level in dB.
final class AudioWriter: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var file: AVAudioFile?
    private let limit: Double
    private var frames: AVAudioFramePosition = 0
    private var started = false
    private var power = -160.0
    private(set) var failure: String?
    let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!

    init(url: URL, limit: Double) {
        self.limit = limit
        var made: AVAudioFile?
        do {
            made = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16_000,
                                                               AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16,
                                                               AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false],
                                   commonFormat: .pcmFormatInt16, interleaved: true)
        } catch { failure = error.localizedDescription }
        file = made
    }

    var recording: Bool { lock.lock(); defer { lock.unlock() }; return started }
    var seconds: Double { lock.lock(); defer { lock.unlock() }; return Double(frames) / 16_000 }
    var full: Bool { seconds >= limit }
    var level: Double { lock.lock(); defer { lock.unlock() }; return power }

    /// Closes the WAV (its header is written when the file goes), so it can be read.
    func finish() { lock.lock(); file = nil; lock.unlock() }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        lock.lock(); let file = self.file; lock.unlock()
        guard let file = file, !full else { return }
        let count = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard count > 0, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: count) else { return }
        buffer.frameLength = count
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(sampleBuffer, at: 0, frameCount: Int32(count),
                                                                  into: buffer.mutableAudioBufferList)
        guard status == noErr, let samples = buffer.int16ChannelData?[0] else { return }
        var sum = 0.0
        for index in 0..<Int(count) { let value = Double(samples[index]) / 32768; sum += value * value }
        let rms = (sum / Double(count)).squareRoot()
        do { try file.write(from: buffer) } catch {
            lock.lock(); failure = error.localizedDescription; lock.unlock(); return
        }
        lock.lock()
        frames += AVAudioFramePosition(count)
        started = true
        power = rms > 0 ? max(-160, 20 * log10(rms)) : -160
        lock.unlock()
    }
}

/// Records `seconds` of the phone's audio into `url` (WAV, 16-bit mono at 16 kHz). Returns how long the capture took
/// to start, in milliseconds, and how many seconds were recorded. With `live`, it reports the start and the level as
/// it goes, and `stop` ends it early. With `frames`, it also writes the phone's picture there and says
/// {"event":"frame","seq","path","t"} for each (FrameWriter).
func record(_ device: AVCaptureDevice, seconds: Double, to url: URL, live: Bool = false,
            stop: StopSignal? = nil, frames: URL? = nil) -> (Int, Double) {
    let session = AVCaptureSession()
    let input: AVCaptureDeviceInput
    do {
        input = try AVCaptureDeviceInput(device: device)
    } catch {
        let code = (error as NSError).code
        if code == AVError.applicationIsNotAuthorizedToUseDevice.rawValue { fail("permission", PERMISSION_MESSAGE) }
        fail("capture_failed", "Mobster couldn't open the iPhone's audio (\(error.localizedDescription)).")
    }
    guard session.canAddInput(input) else { fail("capture_failed", "Mobster couldn't open the iPhone's audio.") }
    session.addInput(input)
    if let frames = frames {
        return recordWithFrames(session, seconds: seconds, to: url, live: live, stop: stop, frames: frames)
    }
    let output = AVCaptureAudioFileOutput()
    output.audioSettings = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16_000, AVNumberOfChannelsKey: 1,
                            AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false,
                            AVLinearPCMIsNonInterleaved: false]
    output.maxRecordedDuration = CMTime(seconds: seconds, preferredTimescale: 600)
    guard session.canAddOutput(output) else { fail("capture_failed", "Mobster couldn't record the iPhone's audio.") }
    session.addOutput(output)
    guard output.connection(with: .audio) != nil else {
        fail("capture_failed", "The iPhone offered no audio to record.")
    }
    let started = Date()
    session.startRunning()
    let recorder = Recorder()
    output.startRecording(to: url, outputFileType: .wav, recordingDelegate: recorder)
    let waited = milliseconds(since: started)
    var announced = false
    var stopping = false
    var nextLevel = Date()
    let finished = pump(timeout: seconds + 10) {
        if recorder.done { return true }
        if live && !announced && recorder.recording {
            announced = true
            emit(["event": "recording", "startMs": milliseconds(since: started)])
        }
        if live && announced && Date() >= nextLevel {
            nextLevel = Date().addingTimeInterval(0.5)
            let channels = output.connection(with: .audio)?.audioChannels ?? []
            let level = channels.map { Double($0.averagePowerLevel) }.max() ?? -160
            emit(["event": "level", "t": (output.recordedDuration.seconds * 10).rounded() / 10,
                  "db": (max(-160, level) * 10).rounded() / 10])
        }
        if let stop = stop, stop.isSet, !stopping {
            stopping = true
            output.stopRecording()
        }
        return false
    }
    if !finished {
        output.stopRecording()
        pump(timeout: 3) { recorder.done }
    }
    let recorded = output.recordedDuration.seconds
    session.stopRunning()
    if let error = recorder.error {
        fail("capture_failed", "The recording stopped early (\(error.localizedDescription)).")
    }
    return (waited, recorded.isFinite ? (recorded * 10).rounded() / 10 : seconds)
}

/// record() for --frames: the sound through AudioWriter and the picture through FrameWriter, on one session. Says
/// {"event":"recording"} at the first sound, {"event":"level"} every half second and {"event":"frame"} for each picture.
func recordWithFrames(_ session: AVCaptureSession, seconds: Double, to url: URL, live: Bool, stop: StopSignal?,
                      frames: URL) -> (Int, Double) {
    let audio = AVCaptureAudioDataOutput()
    audio.audioSettings = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16_000, AVNumberOfChannelsKey: 1,
                           AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false,
                           AVLinearPCMIsNonInterleaved: false]
    let sound = AudioWriter(url: url, limit: seconds)
    if let failure = sound.failure { fail("capture_failed", "Mobster couldn't record the iPhone's audio (\(failure)).") }
    guard session.canAddOutput(audio) else { fail("capture_failed", "Mobster couldn't record the iPhone's audio.") }
    session.addOutput(audio)
    audio.setSampleBufferDelegate(sound, queue: DispatchQueue(label: "phone-audio.sound"))
    // The picture is a bonus: without it WATCH falls back to WebDriverAgent's frames, and the sound still records.
    let video = AVCaptureVideoDataOutput()
    video.alwaysDiscardsLateVideoFrames = true
    video.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
    let writer = FrameWriter(folder: frames)
    if session.canAddOutput(video) {
        session.addOutput(video)
        video.setSampleBufferDelegate(writer, queue: DispatchQueue(label: "phone-audio.frames"))
    }
    let started = Date()
    session.startRunning()
    var waited: Int?
    var nextLevel = Date()
    pump(timeout: seconds + 10) {
        if waited == nil && sound.recording {
            waited = milliseconds(since: started)
            if live { emit(["event": "recording", "startMs": waited!]) }
        }
        if live, let (seq, path) = writer.take() {
            emit(["event": "frame", "seq": seq, "path": path, "t": (Date().timeIntervalSince(started) * 100).rounded() / 100])
        }
        if live && waited != nil && Date() >= nextLevel {
            nextLevel = Date().addingTimeInterval(0.5)
            emit(["event": "level", "t": (sound.seconds * 10).rounded() / 10, "db": (sound.level * 10).rounded() / 10])
        }
        return sound.full || stop?.isSet == true || sound.failure != nil
    }
    session.stopRunning()
    sound.finish()
    if let failure = sound.failure { fail("capture_failed", "The recording stopped early (\(failure)).") }
    return (waited ?? milliseconds(since: started), (sound.seconds * 10).rounded() / 10)
}

/// The loudest sample in decibels below full scale, so a caller can tell silence (nothing played) from speech it
/// couldn't understand. nil when the file can't be read.
func peakDecibels(_ url: URL) -> Double? {
    guard let file = try? AVAudioFile(forReading: url),
          let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)),
          (try? file.read(into: buffer)) != nil, let channel = buffer.floatChannelData?[0] else { return nil }
    var peak: Float = 0
    for index in 0..<Int(buffer.frameLength) { peak = max(peak, abs(channel[index])) }
    return peak > 0 ? (Double(20 * log10(peak)) * 10).rounded() / 10 : -160
}

// MARK: - Speech

/// What was said: the text, and the same text in timed pieces ("start" and "end" in seconds from the audio's start).
struct Heard { let text: String; let segments: [[String: Any]]; let engine: String; let locale: String }

func segment(_ start: Double, _ end: Double, _ text: String) -> [String: Any] {
    ["start": (start * 10).rounded() / 10, "end": (end * 10).rounded() / 10,
     "text": text.split(whereSeparator: \.isWhitespace).joined(separator: " ")]
}

enum SpeechFailure: Error { case noEngine(String), denied, failed(String) }

func wanted(_ locale: String?) -> Locale { locale.map { Locale(identifier: $0) } ?? Locale.current }

@available(macOS 26.0, *)
func analyze(_ url: URL, locale: Locale) async throws -> Heard? {
    guard SpeechTranscriber.isAvailable,
          let resolved = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else { return nil }
    let transcriber = SpeechTranscriber(locale: resolved, preset: .transcription)
    switch await AssetInventory.status(forModules: [transcriber]) {
    case .installed:
        break
    case .supported, .downloading:
        // macOS's own model for the language, as Voice uses: usually already on this Mac, so this only reserves it
        // (measured 0.3 s on 11 Oct 2026). A missing model is downloaded from Apple; if that fails, the on-device
        // SFSpeechRecognizer below is tried.
        guard let request = try? await AssetInventory.assetInstallationRequest(supporting: [transcriber]),
              (try? await request.downloadAndInstall()) != nil else {
            if await AssetInventory.status(forModules: [transcriber]) != .installed { return nil }
            break
        }
    default:
        return nil
    }
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    let collect = Task { () -> [(String, CMTimeRange)] in
        var pieces: [(String, CMTimeRange)] = []
        for try await result in transcriber.results where result.isFinal {
            pieces.append((String(result.text.characters), result.range))
        }
        return pieces
    }
    let file = try AVAudioFile(forReading: url)
    if let last = try await analyzer.analyzeSequence(from: file) {
        try await analyzer.finalizeAndFinish(through: last)
    } else {
        await analyzer.cancelAndFinishNow()
    }
    let pieces = try await collect.value
    let text = pieces.map { $0.0 }.joined(separator: " ")
    let segments = pieces.filter { !$0.0.trimmingCharacters(in: .whitespaces).isEmpty }.map { piece in
        segment(piece.1.start.seconds.isFinite ? piece.1.start.seconds : 0,
                piece.1.end.seconds.isFinite ? piece.1.end.seconds : 0, piece.0)
    }
    return Heard(text: text.split(whereSeparator: \.isWhitespace).joined(separator: " "), segments: segments,
                 engine: "speechanalyzer", locale: resolved.identifier(.bcp47))
}

func recognize(_ url: URL, locale: Locale) async throws -> Heard {
    guard let recognizer = SFSpeechRecognizer(locale: locale), recognizer.supportsOnDeviceRecognition else {
        throw SpeechFailure.noEngine("This Mac has no on-device speech recognition for \(locale.identifier).")
    }
    var status = SFSpeechRecognizer.authorizationStatus()
    if status == .notDetermined {
        status = await withCheckedContinuation { (done: CheckedContinuation<SFSpeechRecognizerAuthorizationStatus, Never>) in
            SFSpeechRecognizer.requestAuthorization { done.resume(returning: $0) }
        }
    }
    guard status == .authorized else { throw SpeechFailure.denied }
    let request = SFSpeechURLRecognitionRequest(url: url)
    request.requiresOnDeviceRecognition = true
    request.shouldReportPartialResults = false
    request.addsPunctuation = true
    let heard: (String, [[String: Any]]) = try await withCheckedThrowingContinuation { done in
        var finished = false
        recognizer.recognitionTask(with: request) { result, error in
            guard !finished else { return }
            if let result = result, result.isFinal {
                finished = true
                done.resume(returning: (result.bestTranscription.formattedString,
                                        phrases(result.bestTranscription.segments)))
            } else if let error = error as NSError? {
                finished = true
                // "No speech detected" is an answer: nothing was said.
                if error.domain == "kAFAssistantErrorDomain" && error.code == 1110 {
                    done.resume(returning: ("", []))
                } else {
                    done.resume(throwing: SpeechFailure.failed(error.localizedDescription))
                }
            }
        }
    }
    return Heard(text: heard.0, segments: heard.1, engine: "sfspeech", locale: locale.identifier)
}

/// SFSpeech's words, timed, as phrases: a new one after a pause of 0.7 s or more, or after 14 words.
func phrases(_ words: [SFTranscriptionSegment]) -> [[String: Any]] {
    var out: [[String: Any]] = []
    var current: [SFTranscriptionSegment] = []
    func close() {
        guard let first = current.first, let last = current.last else { return }
        out.append(segment(first.timestamp, last.timestamp + last.duration,
                           current.map { $0.substring }.joined(separator: " ")))
        current = []
    }
    for word in words {
        if let last = current.last, word.timestamp - (last.timestamp + last.duration) >= 0.7 || current.count >= 14 {
            close()
        }
        current.append(word)
    }
    close()
    return out
}

func transcribe(_ url: URL, locale: String?) async throws -> Heard {
    if #available(macOS 26.0, *), let heard = try await analyze(url, locale: wanted(locale)) {
        return heard
    }
    return try await recognize(url, locale: wanted(locale))
}

func transcribed(_ url: URL, locale: String?) -> (Heard, Int) {
    let started = Date()
    let box = SendableBox<Result<Heard, Error>>()
    Task.detached {
        do { box.value = .success(try await transcribe(url, locale: locale)) } catch { box.value = .failure(error) }
    }
    pump(timeout: 300) { box.value != nil }
    switch box.value {
    case .success(let heard)?: return (heard, milliseconds(since: started))
    case .failure(SpeechFailure.noEngine(let message))?: fail("no_engine", message)
    case .failure(SpeechFailure.denied)?:
        fail("speech_denied", "macOS didn't let Mobster use speech recognition. Allow it in System Settings › Privacy & Security › Speech Recognition.")
    case .failure(let error)?: fail("transcribe_failed", "Mobster couldn't turn the audio into text (\(error.localizedDescription)).")
    case nil: fail("transcribe_failed", "Mobster couldn't turn the audio into text.")
    }
}

final class SendableBox<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: Value?
    var value: Value? {
        get { lock.lock(); defer { lock.unlock() }; return stored }
        set { lock.lock(); stored = newValue; lock.unlock() }
    }
}

// MARK: - Sounds

/// A label below this confidence isn't reported; at most this many per window, the surest first.
let SOUND_FLOOR = 0.3
let SOUND_LABELS = 6
/// The classifier's window and how far it moves (half a window: SoundAnalysis's default overlap).
let SOUND_WINDOW = 3.0

/// The classifier's windows as they come: start, length and the labels at least SOUND_FLOOR sure.
final class SoundWindows: NSObject, SNResultsObserving, @unchecked Sendable {
    private let lock = NSLock()
    private var rows: [[String: Any]] = []
    private var failed: String?
    var windows: [[String: Any]] { lock.lock(); defer { lock.unlock() }; return rows }
    var failure: String? { lock.lock(); defer { lock.unlock() }; return failed }

    func request(_ request: SNRequest, didProduce result: SNResult) {
        guard let result = result as? SNClassificationResult else { return }
        var labels: [String: Double] = [:]
        for item in result.classifications.prefix(SOUND_LABELS) where item.confidence >= SOUND_FLOOR {
            labels[item.identifier] = (item.confidence * 100).rounded() / 100
        }
        let start = result.timeRange.start.seconds, length = result.timeRange.duration.seconds
        guard start.isFinite, length.isFinite else { return }
        lock.lock()
        rows.append(["t": (start * 10).rounded() / 10, "d": (length * 10).rounded() / 10, "labels": labels])
        lock.unlock()
    }

    func request(_ request: SNRequest, didFailWithError error: Error) {
        lock.lock()
        failed = error.localizedDescription
        lock.unlock()
    }
}

/// The sounds in `url`, classified on this Mac: {"soundWindows","classifyMs"}, or {"soundsError","classifyMs"}.
/// Synchronous: listen runs it on its own thread while the speech is transcribed.
func classifySounds(_ url: URL) -> [String: Any] {
    let started = Date()
    guard #available(macOS 12.0, *) else {
        return ["soundsError": "Telling sounds apart needs macOS 12 or later.", "classifyMs": 0]
    }
    do {
        let file = try AVAudioFile(forReading: url)
        let rate = file.processingFormat.sampleRate
        let seconds = rate > 0 ? Double(file.length) / rate : 0
        guard seconds >= 0.5 else { return ["soundWindows": [], "classifyMs": milliseconds(since: started)] }
        let request = try SNClassifySoundRequest(classifierIdentifier: .version1)
        request.windowDuration = CMTime(seconds: min(SOUND_WINDOW, seconds), preferredTimescale: 48_000)
        request.overlapFactor = 0.5
        let analyzer = try SNAudioFileAnalyzer(url: url)
        let windows = SoundWindows()
        try analyzer.add(request, withObserver: windows)
        analyzer.analyze()
        if let failure = windows.failure {
            return ["soundsError": "The sound classifier failed (\(failure)).",
                    "classifyMs": milliseconds(since: started)]
        }
        return ["soundWindows": windows.windows, "classifyMs": milliseconds(since: started)]
    } catch {
        return ["soundsError": "The sound classifier couldn't read the audio (\(error.localizedDescription)).",
                "classifyMs": milliseconds(since: started)]
    }
}

/// classifySounds on a thread of its own; the box holds its answer when it is done.
func classifyingSounds(_ url: URL) -> SendableBox<[String: Any]> {
    let box = SendableBox<[String: Any]>()
    Thread.detachNewThread { box.value = classifySounds(url) }
    return box
}

// MARK: - Commands

switch command {
case "record-video":
    guard let text = option("--seconds"), let seconds = Double(text), seconds.isFinite,
          seconds >= 1, seconds <= 300, let udid = option("--udid"), !udid.isEmpty,
          let work = option("--work") else { usage() }
    let folder = URL(fileURLWithPath: work)
    let attributes = try? FileManager.default.attributesOfItem(atPath: folder.path)
    guard attributes?[.type] as? FileAttributeType == .typeDirectory,
          attributes?[.posixPermissions] as? Int == 0o700,
          attributes?[.ownerAccountID] as? UInt32 == getuid() else {
        fail("capture_failed", "The video working folder must be owned by you with permissions 0700.")
    }
    let video = folder.appendingPathComponent("video.mov")
    guard !FileManager.default.fileExists(atPath: video.path) else {
        fail("capture_failed", "The video working file already exists. Choose an empty private folder.")
    }
    cleanup.append(video)
    allowScreenCaptureDevices()
    let name = option("--name")
    let (found, picked) = findPhone(udid: udid, name: name, phones: option("--phones").flatMap { Int($0) }, timeout: 5)
    guard let phone = found, case .found(_, let match) = picked else { refuse(picked, name: name) }
    authorize(live: false)
    let audio = recordVideo(phone, seconds: seconds, to: video)
    guard let size = (try? FileManager.default.attributesOfItem(atPath: video.path))?[.size] as? UInt64,
          size > 0 else { fail("capture_failed", "The iPhone produced no video recording.") }
    let durationBox = SendableBox<Double>()
    Task.detached {
        do { durationBox.value = try await AVURLAsset(url: video).load(.duration).seconds } catch { durationBox.value = -1 }
    }
    guard pump(timeout: 5, until: { durationBox.value != nil }), let duration = durationBox.value,
          duration.isFinite, duration > 0 else { fail("capture_failed", "The recorded movie has no readable duration.") }
    do { try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: video.path) } catch {
        fail("capture_failed", "Mobster couldn't protect the recorded video file.")
    }
    cleanup = []
    emit(["ok": true, "seconds": seconds, "durationSeconds": duration, "audio": audio, "bytes": size, "match": match])

case "devices":
    allowScreenCaptureDevices()
    pump(timeout: 2) { false }   // give them a moment to appear
    emit(["ok": true, "devices": iphones().map { ["id": $0.uniqueID, "name": $0.localizedName, "model": $0.modelID] }])

case "match":
    // Which device listen would pick from the list on stdin ([{"id","name","model"}]), by the same rules: for tests
    // and diagnosis. Nothing is captured.
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard let rows = (try? JSONSerialization.jsonObject(with: input)) as? [[String: Any]] else { usage() }
    let listed = rows.map { Candidate(id: $0["id"] as? String ?? "", name: $0["name"] as? String ?? "",
                                      model: $0["model"] as? String ?? "") }
    let name = option("--name")
    switch pick(listed, udid: option("--udid"), name: name, phones: option("--phones").flatMap { Int($0) }) {
    case .found(let index, let how): emit(["ok": true, "index": index, "match": how])
    case let other: refuse(other, name: name)
    }

case "listen":
    guard let text = option("--seconds"), let seconds = Double(text), seconds >= 1, seconds <= 120 else { usage() }
    let keep = option("--keep")
    let live = arguments.contains("--live")
    let stop = StopSignal()
    if live { watchStdin(stop) }
    let looking = Date()
    allowScreenCaptureDevices()
    let name = option("--name")
    let (found, picked) = findPhone(udid: option("--udid"), name: name, phones: option("--phones").flatMap { Int($0) },
                                    timeout: 5)
    guard let phone = found, case .found(_, let match) = picked else { refuse(picked, name: name) }
    let findMs = milliseconds(since: looking)
    authorize(live: live, stop: live ? stop : nil)
    // The caller's private folder (--work), which it deletes itself, even if this process is killed; else our own.
    let folder: URL
    if let work = option("--work") {
        folder = URL(fileURLWithPath: work).appendingPathComponent("capture-\(UUID().uuidString)")
    } else {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("mobster-listen-\(UUID().uuidString)")
    }
    try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                             attributes: [.posixPermissions: 0o700])
    cleanup.append(folder)
    let audio = folder.appendingPathComponent("audio.wav")
    let started = Date()
    var frames: URL?
    if live, arguments.contains("--frames") {
        frames = folder.appendingPathComponent("frames")
        try? FileManager.default.createDirectory(at: frames!, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
    }
    let (waited, recorded) = record(phone, seconds: seconds, to: audio, live: live, stop: stop, frames: frames)
    let recordMs = milliseconds(since: started)
    let peak = peakDecibels(audio)
    let sounds = classifyingSounds(audio)
    let (heard, transcribeMs) = transcribed(audio, locale: option("--locale"))
    pump(timeout: 60) { sounds.value != nil }
    var answer: [String: Any] = ["ok": true, "text": heard.text, "segments": heard.segments, "engine": heard.engine,
                                 "locale": heard.locale, "seconds": seconds, "recordedSeconds": recorded,
                                 "recordMs": recordMs, "startMs": waited, "findMs": findMs,
                                 "transcribeMs": transcribeMs, "match": match]
    if let peak = peak { answer["peakDb"] = peak }
    answer.merge(sounds.value ?? ["soundsError": "The sound classifier took too long."]) { mine, _ in mine }
    if let keep = keep {
        let target = URL(fileURLWithPath: keep)
        try? FileManager.default.removeItem(at: target)
        if (try? FileManager.default.moveItem(at: audio, to: target)) != nil { answer["kept"] = target.path }
    }
    removeTemporary()
    emit(answer)

case "transcribe":
    guard let path = arguments.first, !path.hasPrefix("--") else { usage() }
    let url = URL(fileURLWithPath: path)
    guard FileManager.default.isReadableFile(atPath: url.path) else { fail("transcribe_failed", "No audio file at \(path).") }
    let (heard, transcribeMs) = transcribed(url, locale: option("--locale"))
    emit(["ok": true, "text": heard.text, "segments": heard.segments, "engine": heard.engine, "locale": heard.locale,
          "transcribeMs": transcribeMs])

case "classify":
    guard let path = arguments.first, !path.hasPrefix("--") else { usage() }
    guard FileManager.default.isReadableFile(atPath: path) else { fail("classify_failed", "No audio file at \(path).") }
    var answer: [String: Any] = ["ok": true]
    answer.merge(classifySounds(URL(fileURLWithPath: path))) { mine, _ in mine }
    emit(answer)

case "prepare":
    let locale = wanted(option("--locale"))
    let box = SendableBox<[String: Any]>()
    Task.detached {
        guard #available(macOS 26.0, *), SpeechTranscriber.isAvailable else {
            box.value = ["ok": false, "error": "no_engine", "message": "On-device speech models need macOS 26 or later."]
            return
        }
        guard let resolved = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            box.value = ["ok": false, "error": "no_engine", "message": "This language isn't available on this Mac."]
            return
        }
        let transcriber = SpeechTranscriber(locale: resolved, preset: .transcription)
        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                try await request.downloadAndInstall()
            }
            box.value = ["ok": true, "speechAnalyzer": "installed", "locale": resolved.identifier(.bcp47)]
        } catch {
            box.value = ["ok": false, "error": "prepare_failed",
                         "message": "macOS couldn't install its speech model (\(error.localizedDescription))."]
        }
    }
    pump(timeout: 900) { box.value != nil }
    emit(box.value ?? ["ok": false, "error": "prepare_failed", "message": "macOS took too long to install its speech model."])

case "status":
    let locale = wanted(option("--locale"))
    let box = SendableBox<[String: Any]>()
    Task.detached {
        var answer: [String: Any] = ["ok": true, "speechAnalyzer": "unavailable"]
        if #available(macOS 26.0, *), SpeechTranscriber.isAvailable {
            if let resolved = await SpeechTranscriber.supportedLocale(equivalentTo: locale) {
                let transcriber = SpeechTranscriber(locale: resolved, preset: .transcription)
                switch await AssetInventory.status(forModules: [transcriber]) {
                case .installed: answer["speechAnalyzer"] = "installed"
                case .downloading: answer["speechAnalyzer"] = "downloading"
                case .supported: answer["speechAnalyzer"] = "supported"
                default: answer["speechAnalyzer"] = "unsupported"
                }
                answer["locale"] = resolved.identifier(.bcp47)
            } else {
                answer["speechAnalyzer"] = "unsupported"
            }
        }
        let names: [SFSpeechRecognizerAuthorizationStatus: String] = [.authorized: "granted", .denied: "denied",
                                                                      .restricted: "restricted", .notDetermined: "not_determined"]
        answer["sfSpeech"] = ["authorization": names[SFSpeechRecognizer.authorizationStatus()] ?? "unknown",
                              "onDevice": SFSpeechRecognizer(locale: locale)?.supportsOnDeviceRecognition ?? false]
        answer["capture"] = ["camera": authorizationName(AVCaptureDevice.authorizationStatus(for: .video)),
                             "microphone": authorizationName(AVCaptureDevice.authorizationStatus(for: .audio))]
        box.value = answer
    }
    pump(timeout: 30) { box.value != nil }
    emit(box.value ?? ["ok": false, "error": "status_failed", "message": "Mobster couldn't read this Mac's speech recognition."])

default:
    usage()
}
