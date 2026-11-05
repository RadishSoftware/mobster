// mirroring: drives an iPhone through macOS iPhone Mirroring, for mobile_agent/mirroring.py (an experimental
// connection: no cable, no Xcode, nothing installed on the phone).
//
// It reads one JSON request per line on standard input and prints one JSON line per request:
//   {"op":"state"}                         where the Mirroring window is, and whether it shows the phone:
//                                          {"ok":true,"state":"ready"|"blocked"|"no-window"|"not-running",
//                                           "window":{"x","y","w","h","id"},"said":[...]} ("said": the Mac's own
//                                          words on an interstitial such as "iPhone in Use"; never the phone's)
//   {"op":"capture","path":P,"ocr":true}  the window as a JPEG at P (ScreenCaptureKit), its pixel hash, how much
//                                          of it is black, and with "ocr" the text Vision reads on it:
//                                          {"rows":[{"text","confidence","rect":[x,y,w,h]}]}, rects as fractions
//                                          of the window from its top left
//   {"op":"tap","x":fx,"y":fy}            a tap at a point given as fractions of the window
//   {"op":"press","x","y","ms"}            a long press
//   {"op":"drag","x1","y1","x2","y2","ms"} a touch drag (iOS sees a swipe)
//   {"op":"scroll","x","y","dy","dx"}      a scroll gesture at a point, in window points (dy > 0 reveals what is below)
//   {"op":"key","combo":"cmd+1"}          a key or shortcut (cmd+1 Home, cmd+2 App Switcher, cmd+3 Spotlight)
//   {"op":"type","text":T,"mode":"keys"|"paste"}  text into the focused field: US key codes, or the Mac pasteboard
//                                          and cmd+v
//   `mirroring stream FPS MAX_WIDTH QUALITY` (a separate process): the window as JPEG frames, for the live view
//   `mirroring drop FX FY PATH...` / `mirroring drag-out FX FY DEST` (separate processes): files to and from the phone
//   `mirroring audio --seconds N --out FILE [--live]` (a separate process): the phone's sound, for hearing
//   {"op":"restore"}                       gives the front back to the app that had it before the first gesture
//
// A gesture request may carry "input":"foreground" (the person set MOBSTER_MIRRORING_INPUT=foreground); without it
// the gesture goes in the background and the answer says which way it went: {"ok":true,"input":"background"}.
//
// Errors are {"ok":false,"error":"...","code":"..."}: "not_running", "no_window", "blocked", "covered" (foreground
// input only: another window would take the gesture, so nothing was sent), "background" (the gesture can't be
// addressed to the window, or Mirroring isn't answering, so nothing was sent), "permission" (Screen Recording or
// Accessibility is missing), "capture", "unsupported" (macOS before 15), "usage". A gesture is sent at most once and
// never retried here, and a failed background gesture never turns into a foreground one.
//
// The phone image is a video stream: macOS accessibility sees nothing inside it, so input goes in as mouse and
// keyboard events at the window's position. In the background they are posted to the Mirroring process and addressed
// to its window: no cursor move, no focus change, and the window can be covered. Foreground input brings the window
// to the front and posts to the HID event tap. Capture works with the window covered either way.
//
// The Mac app carries it as Contents/Resources/mirroring; a source checkout builds it with
// native_helpers.build("mirroring").

import AVFoundation
import AppKit
import CoreImage
import CoreMedia
import ApplicationServices
import CoreGraphics
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers
import Vision

let bundleID = "com.apple.ScreenContinuity"
let appPath = "/System/Applications/iPhone Mirroring.app"

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

// MARK: - the app and its window

struct Window {
    let id: CGWindowID
    let frame: CGRect  // global screen points, top-left origin (the space CGEvent uses)
    var json: [String: Any] {
        ["x": frame.origin.x, "y": frame.origin.y, "w": frame.width, "h": frame.height, "id": Int(id)]
    }
}

func runningApp() -> NSRunningApplication? {
    NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first
}

func windowList(onScreenOnly: Bool) -> [[String: Any]] {
    let options: CGWindowListOption = onScreenOnly ? [.optionOnScreenOnly, .excludeDesktopElements] : [.optionAll]
    return CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
}

// The phone window: the app's normal-layer window whose title is the app's own name (both localized together), else
// its frontmost normal-layer window. Matched by process, never by the localized owner name.
func phoneWindow(_ app: NSRunningApplication) -> Window? {
    let pid = app.processIdentifier
    let mine = windowList(onScreenOnly: false).filter {
        ($0[kCGWindowOwnerPID as String] as? pid_t) == pid && ($0[kCGWindowLayer as String] as? Int ?? 1) == 0
    }
    let named = mine.filter {
        let name = $0[kCGWindowName as String] as? String
        return name != nil && name == ($0[kCGWindowOwnerName as String] as? String)
    }
    for info in (named.isEmpty ? mine : named) {
        guard let bounds = info[kCGWindowBounds as String] as? [String: Any],
              let rect = CGRect(dictionaryRepresentation: bounds as CFDictionary),
              let number = info[kCGWindowNumber as String] as? Int,
              rect.width > 40, rect.height > 80 else { continue }
        return Window(id: CGWindowID(number), frame: rect)
    }
    return nil
}

func attribute(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: AnyObject?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

func axFrame(_ element: AXUIElement) -> CGRect? {
    guard let position = attribute(element, kAXPositionAttribute), let size = attribute(element, kAXSizeAttribute)
    else { return nil }
    var point = CGPoint.zero, extent = CGSize.zero
    guard AXValueGetValue(position as! AXValue, .cgPoint, &point), AXValueGetValue(size as! AXValue, .cgSize, &extent)
    else { return nil }
    return CGRect(origin: point, size: extent)
}

func axWindow(_ app: NSRunningApplication, _ window: Window) -> AXUIElement? {
    let element = AXUIElementCreateApplication(app.processIdentifier)
    let windows = attribute(element, kAXWindowsAttribute) as? [AXUIElement] ?? []
    return windows.first { candidate in
        guard let frame = axFrame(candidate) else { return false }
        return abs(frame.minX - window.frame.minX) < 4 && abs(frame.minY - window.frame.minY) < 4
    }
}

// The Mac's own views inside the phone window. A live session has none (the phone is a video stream); "iPhone in
// Use", "Paused", "Connect" and the Mac login gate are ordinary views with words and buttons.
func interstitial(_ app: NSRunningApplication, _ window: Window) -> (blocked: Bool, said: [String], login: Bool) {
    guard let element = axWindow(app, window) else { return (false, [], false) }
    var said: [String] = []
    var found = false
    var login = false
    func walk(_ node: AXUIElement, _ depth: Int) {
        if depth > 8 || said.count >= 8 { return }
        let role = attribute(node, kAXRoleAttribute) as? String ?? ""
        if ["AXStaticText", "AXButton", "AXTextField", "AXSecureTextField"].contains(role) {
            found = true
            if role == "AXSecureTextField" || role == "AXTextField" { login = true }
            if role == "AXStaticText", let value = attribute(node, kAXValueAttribute) as? String {
                let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty { said.append(String(text.prefix(160))) }
            }
        }
        for child in attribute(node, kAXChildrenAttribute) as? [AXUIElement] ?? [] { walk(child, depth + 1) }
    }
    walk(element, 0)
    return (found, said, login)
}

func state() -> [String: Any] {
    guard let app = runningApp() else { return ["ok": true, "state": "not-running"] }
    guard let window = phoneWindow(app) else { return ["ok": true, "state": "no-window"] }
    let seen = interstitial(app, window)
    var out: [String: Any] = ["ok": true, "state": seen.blocked ? "blocked" : "ready", "window": window.json,
                              "front": isFront(app)]
    if seen.blocked {
        out["said"] = seen.said
        out["login"] = seen.login
    }
    return out
}

func readyWindow() throws -> (NSRunningApplication, Window) {
    guard let app = runningApp() else {
        throw HelperError(code: "not_running", message: "iPhone Mirroring isn't open")
    }
    guard let window = phoneWindow(app) else {
        throw HelperError(code: "no_window", message: "iPhone Mirroring has no phone window")
    }
    return (app, window)
}

// MARK: - the front

var previousFront: pid_t? = nil

func frontmostOwner() -> pid_t? {
    for info in windowList(onScreenOnly: true) where (info[kCGWindowLayer as String] as? Int ?? 1) == 0 {
        return info[kCGWindowOwnerPID as String] as? pid_t
    }
    return nil
}

// In front means both: the app has keyboard focus (AXFrontmost) and its window is the frontmost one. Either alone
// has been seen without the other, and then keystrokes or clicks land in another app.
func isFront(_ app: NSRunningApplication) -> Bool {
    let element = AXUIElementCreateApplication(app.processIdentifier)
    let front = attribute(element, kAXFrontmostAttribute) as? Bool ?? false
    return front && frontmostOwner() == app.processIdentifier
}

func bringToFront(_ app: NSRunningApplication, _ window: Window) throws {
    if isFront(app) { return }
    if previousFront == nil, let owner = frontmostOwner(), owner != app.processIdentifier { previousFront = owner }
    let element = axWindow(app, window)
    if let element, attribute(element, kAXMinimizedAttribute) as? Bool == true {
        AXUIElementSetAttributeValue(element, kAXMinimizedAttribute as CFString, kCFBooleanFalse)
    }
    var deadline = Date().addingTimeInterval(1.5)
    while Date() < deadline {
        app.activate()
        if let element { AXUIElementPerformAction(element, kAXRaiseAction as CFString) }
        Thread.sleep(forTimeInterval: 0.08)
        if isFront(app) { return }
    }
    // macOS 14's cooperative activation can refuse a background process; Launch Services' open doesn't.
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    process.arguments = ["-a", appPath]
    try? process.run()
    process.waitUntilExit()
    deadline = Date().addingTimeInterval(1.5)
    while Date() < deadline {
        Thread.sleep(forTimeInterval: 0.1)
        if isFront(app) { return }
    }
    throw HelperError(code: "covered", message: "iPhone Mirroring couldn't come to the front, so nothing was sent")
}

// The topmost normal window at a global point belongs to iPhone Mirroring: a click there reaches the phone.
func ownsPoint(_ app: NSRunningApplication, _ point: CGPoint) -> Bool {
    for info in windowList(onScreenOnly: true) where (info[kCGWindowLayer as String] as? Int ?? 1) == 0 {
        guard let bounds = info[kCGWindowBounds as String] as? [String: Any],
              let rect = CGRect(dictionaryRepresentation: bounds as CFDictionary), rect.contains(point) else { continue }
        return (info[kCGWindowOwnerPID as String] as? pid_t) == app.processIdentifier
    }
    return false
}

func restoreFront() -> [String: Any] {
    if let pid = previousFront, let app = NSRunningApplication(processIdentifier: pid) {
        app.activate(options: [])
    }
    previousFront = nil
    return ["ok": true]
}

// MARK: - capture

final class Box<T>: @unchecked Sendable { var value: T; init(_ value: T) { self.value = value } }

var cachedWindow: SCWindow? = nil

func scWindow(_ id: CGWindowID) throws -> SCWindow {
    if let cached = cachedWindow, cached.windowID == id { return cached }
    let done = DispatchSemaphore(value: 0)
    let result = Box<(SCShareableContent?, Error?)>((nil, nil))
    SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, error in
        result.value = (content, error)
        done.signal()
    }
    if done.wait(timeout: .now() + 5) == .timedOut {
        throw HelperError(code: "capture", message: "Screen capture didn't answer")
    }
    guard let content = result.value.0 else {
        throw HelperError(code: "permission", message: "Screen Recording permission is needed to read the phone")
    }
    guard let window = content.windows.first(where: { $0.windowID == id }) else {
        throw HelperError(code: "no_window", message: "The Mirroring window isn't capturable")
    }
    cachedWindow = window
    return window
}

@available(macOS 14.0, *)
func captureImage(_ window: Window) throws -> CGImage {
    let target = try scWindow(window.id)
    let filter = SCContentFilter(desktopIndependentWindow: target)
    let config = SCStreamConfiguration()
    let scale = CGFloat(filter.pointPixelScale)
    config.width = Int(window.frame.width * scale)
    config.height = Int(window.frame.height * scale)
    config.showsCursor = false
    config.ignoreShadowsSingleWindow = true
    let done = DispatchSemaphore(value: 0)
    let result = Box<(CGImage?, Error?)>((nil, nil))
    SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) { image, error in
        result.value = (image, error)
        done.signal()
    }
    if done.wait(timeout: .now() + 5) == .timedOut {
        throw HelperError(code: "capture", message: "The window capture didn't finish")
    }
    guard let image = result.value.0 else {
        cachedWindow = nil
        throw HelperError(code: "capture", message: "The window capture failed")
    }
    return image
}

// A small grey picture of the frame: its hash says whether the screen changed; its share of near-black cells says
// whether protected video (DRM) is drawn black.
func signature(_ image: CGImage) -> (hash: String, dark: Double) {
    let width = 24, height = 52
    var pixels = [UInt8](repeating: 0, count: width * height)
    let context = CGContext(data: &pixels, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width,
                            space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)
    context?.interpolationQuality = .low
    context?.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    var hash: UInt64 = 1469598103934665603
    var dark = 0
    for value in pixels {
        hash = (hash ^ UInt64(value >> 3)) &* 1099511628211
        if value < 10 { dark += 1 }
    }
    return (String(format: "%016llx", hash), Double(dark) / Double(pixels.count))
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

func recognize(_ image: CGImage) throws -> [[String: Any]] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    return (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return ["text": candidate.string, "confidence": Double(candidate.confidence),
                "rect": [box.minX, 1 - box.maxY, box.width, box.height]]
    }
}

func capture(_ request: [String: Any]) throws -> [String: Any] {
    let (app, window) = try readyWindow()
    let started = Date()
    guard #available(macOS 14.0, *) else { throw HelperError(code: "unsupported", message: "This needs macOS 15") }
    let image = try captureImage(window)
    let captured = Date()
    let sig = signature(image)
    var out: [String: Any] = ["ok": true, "window": window.json, "hash": sig.hash, "dark": sig.dark,
                              "pixels": [image.width, image.height]]
    if let path = request["path"] as? String { try writeJPEG(image, path) }
    if request["ocr"] as? Bool == true {
        out["rows"] = try recognize(image)
    }
    let seen = interstitial(app, window)
    if seen.blocked {
        out["blocked"] = true
        out["said"] = seen.said
        out["login"] = seen.login
    }
    out["ms"] = ["capture": Int(captured.timeIntervalSince(started) * 1000),
                 "total": Int(Date().timeIntervalSince(started) * 1000)]
    return out
}

// MARK: - input

// Two ways in. The default posts each event to the iPhone Mirroring process, addressed to its window: the Mac's
// cursor, keyboard focus and front app stay where the person left them, and the window can be behind other windows.
// The other (`"input":"foreground"`, from MOBSTER_MIRRORING_INPUT=foreground) is the earlier path: it brings the
// window to the front, moves the real cursor and posts to the HID event tap. It is used only when asked for.
//
// Background delivery rests on two things Apple doesn't document, both read off macOS 26:
// CGEvent field 51 holds the number of the window an event is for, and CGEventSetWindowLocation (exported by
// CoreGraphics, absent from its headers) sets where in that window it lands. Without both, AppKit hands
// the app an event with no window and drops it. When either is missing, or the app doesn't answer, nothing is sent
// and the error says so: a later macOS can break this, and it must not fall back to the front on its own.

var foreground = false  // set from each request

struct Target {
    let pid: pid_t
    let window: Window
}

typealias SetWindowLocation = @convention(c) (CGEvent, CGPoint) -> Void
let setWindowLocation: SetWindowLocation? = {
    guard let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "CGEventSetWindowLocation") else { return nil }
    return unsafeBitCast(symbol, to: SetWindowLocation.self)
}()
let windowField = CGEventField(rawValue: 51)!
// A private source starts with no modifier flags and never merges the person's own held keys into what we post.
let source = CGEventSource(stateID: .privateState)

func background(_ message: String) -> HelperError { HelperError(code: "background", message: message) }

let foregroundHint = "Set MOBSTER_MIRRORING_INPUT=foreground to let Mobster bring the window forward and use your cursor and keyboard for each gesture."

// Ready for background input: the window we address is the live one, and its app answers. A frozen Mirroring would
// take a gesture late, and Mobster couldn't say when it landed, so it is refused.
func confirmBackground(_ app: NSRunningApplication, _ window: Window) throws {
    guard setWindowLocation != nil else {
        throw background("This macOS doesn't let Mobster address iPhone Mirroring's window, so nothing was sent. " + foregroundHint)
    }
    guard !app.isTerminated, axWindow(app, window) != nil else {
        throw background("iPhone Mirroring isn't answering, so nothing was sent. Wait for it, or quit and reopen it, then try again. " + foregroundHint)
    }
}

func number(_ request: [String: Any], _ key: String) throws -> Double {
    guard let value = request[key] as? Double ?? (request[key] as? Int).map(Double.init), value.isFinite else {
        throw HelperError(code: "usage", message: "\(key) is missing")
    }
    return value
}

// A point given as fractions of the window, in window points from its top left; refused outside the window.
func localPoint(_ window: Window, _ fx: Double, _ fy: Double) throws -> CGPoint {
    guard (0...1).contains(fx), (0...1).contains(fy) else {
        throw HelperError(code: "usage", message: "The point is outside the phone's screen")
    }
    return CGPoint(x: window.frame.width * fx, y: window.frame.height * fy)
}

func globalPoint(_ target: Target, _ local: CGPoint) -> CGPoint {
    CGPoint(x: target.window.frame.minX + local.x, y: target.window.frame.minY + local.y)
}

// Ready for a gesture: the session shows the phone and Mirroring can take it. In the foreground path the window is
// also in front and owns every point the gesture touches. Otherwise nothing is sent.
func readyForInput(_ fractions: [(Double, Double)] = []) throws -> (target: Target, points: [CGPoint]) {
    let (app, found) = try readyWindow()
    let seen = interstitial(app, found)
    if seen.blocked {
        throw HelperError(code: "blocked", message: seen.said.first ?? "iPhone Mirroring isn't showing the phone")
    }
    guard AXIsProcessTrusted() else {
        throw HelperError(code: "permission", message: "Accessibility permission is needed to tap the phone")
    }
    let target = Target(pid: app.processIdentifier, window: found)
    let locals = try fractions.map { try localPoint(found, $0.0, $0.1) }
    if foreground {
        try bringToFront(app, found)
        for local in locals where !ownsPoint(app, globalPoint(target, local)) {
            throw HelperError(code: "covered", message: "Another window covers the phone, so nothing was sent")
        }
    } else {
        try confirmBackground(app, found)
    }
    return (target, locals)
}

func withCursorKept(_ body: () throws -> Void) rethrows {
    guard foreground else { return try body() }
    let saved = CGEvent(source: nil)?.location
    defer { if let saved { CGWarpMouseCursorPosition(saved) } }
    try body()
}

// One mouse event at a point in the window.
func post(_ type: CGEventType, _ local: CGPoint, _ target: Target) throws {
    let point = globalPoint(target, local)
    if foreground { return postHID(type, point) }
    guard let setLocation = setWindowLocation,
          let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: .left)
    else { throw background("The Mac wouldn't make the mouse event, so nothing was sent. " + foregroundHint) }
    event.setIntegerValueField(windowField, value: Int64(target.window.id))
    setLocation(event, local)
    if type != .mouseMoved { event.setIntegerValueField(.mouseEventClickState, value: 1) }
    event.postToPid(target.pid)
}

// One mouse event at a global point, through the Mac's pointer. A file drag uses it: the drag runs between two apps,
// so it can't be sent to one window.
func postHID(_ type: CGEventType, _ point: CGPoint) {
    CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: .left)?
        .post(tap: .cghidEventTap)
}

func tap(_ request: [String: Any], holdMs: Double = 50) throws -> [String: Any] {
    let (target, points) = try readyForInput([(try number(request, "x"), try number(request, "y"))])
    let point = points[0]
    try withCursorKept {
        try post(.mouseMoved, point, target)
        usleep(foreground ? 40_000 : 10_000)
        try post(.leftMouseDown, point, target)
        usleep(useconds_t(max(holdMs, 20) * 1000))
        try post(.leftMouseUp, point, target)
    }
    return ["ok": true, "input": foreground ? "foreground" : "background"]
}

func drag(_ request: [String: Any]) throws -> [String: Any] {
    let (target, points) = try readyForInput([(try number(request, "x1"), try number(request, "y1")),
                                              (try number(request, "x2"), try number(request, "y2"))])
    let start = points[0], end = points[1]
    let ms = max(120, (request["ms"] as? Double) ?? 350)
    let steps = 16
    try withCursorKept {
        try post(.mouseMoved, start, target)
        usleep(foreground ? 40_000 : 10_000)
        try post(.leftMouseDown, start, target)
        usleep(30_000)
        for step in 1...steps {
            let t = Double(step) / Double(steps)
            try post(.leftMouseDragged, CGPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t), target)
            usleep(useconds_t(ms / Double(steps) * 1000))
        }
        // Held still a moment before lifting, so the lift carries no speed and the content doesn't fling on.
        usleep(60_000)
        try post(.leftMouseUp, end, target)
    }
    return ["ok": true, "input": foreground ? "foreground" : "background"]
}

func scroll(_ request: [String: Any]) throws -> [String: Any] {
    let (target, points) = try readyForInput([(try number(request, "x"), try number(request, "y"))])
    let local = points[0]
    let dy = (request["dy"] as? Double) ?? 0, dx = (request["dx"] as? Double) ?? 0
    let steps = 8
    try withCursorKept {
        try post(.mouseMoved, local, target)
        usleep(foreground ? 40_000 : 10_000)
        for _ in 0..<steps {
            let event = CGEvent(scrollWheelEvent2Source: foreground ? nil : source, units: .pixel, wheelCount: 2,
                                wheel1: Int32(dy / Double(steps)), wheel2: Int32(dx / Double(steps)), wheel3: 0)
            event?.location = globalPoint(target, local)
            if foreground {
                event?.post(tap: .cghidEventTap)
            } else {
                guard let event, let setLocation = setWindowLocation else {
                    throw background("The Mac wouldn't make the scroll event, so nothing was sent. " + foregroundHint)
                }
                event.setIntegerValueField(windowField, value: Int64(target.window.id))
                setLocation(event, local)
                event.postToPid(target.pid)
            }
            usleep(25_000)
        }
    }
    return ["ok": true, "input": foreground ? "foreground" : "background"]
}

// US key codes. iPhone Mirroring forwards raw key codes to the phone and drops both the event's flags and any
// Unicode payload, so a modifier has to be a key held down, and text has to be key codes (or a paste).
let keyCodes: [String: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36, "l": 37,
    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48,
    "space": 49, " ": 49, "`": 50, "delete": 51, "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126,
]
let shifted: [Character: String] = [
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0", "_": "-",
    "+": "=", ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/", "~": "`", "{": "[", "}": "]", "|": "\\",
]
let modifierKeys: [String: (CGKeyCode, CGEventFlags)] = [
    "cmd": (55, .maskCommand), "shift": (56, .maskShift), "alt": (58, .maskAlternate), "ctrl": (59, .maskControl),
]

func key(_ code: CGKeyCode, _ down: Bool, _ flags: CGEventFlags, _ target: Target) throws {
    if foreground {
        let event = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)
        if !flags.isEmpty { event?.flags = flags }
        event?.post(tap: .cghidEventTap)
        return
    }
    guard let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: down) else {
        throw background("The Mac wouldn't make the key event, so nothing was sent. " + foregroundHint)
    }
    event.flags = flags
    event.setIntegerValueField(windowField, value: Int64(target.window.id))
    event.postToPid(target.pid)
}

func press(_ code: CGKeyCode, holding modifiers: [String], _ target: Target) throws {
    var flags: CGEventFlags = []
    var held: [(CGKeyCode, CGEventFlags)] = []
    // Always released, last first, each without its own flag: a modifier left down corrupts every later key.
    func release() {
        for (modCode, mask) in held.reversed() {
            flags.remove(mask)
            try? key(modCode, false, flags, target)
            usleep(10_000)
        }
    }
    do {
        for name in modifiers {
            guard let (modCode, mask) = modifierKeys[name] else { continue }
            flags.insert(mask)
            held.append((modCode, mask))
            try key(modCode, true, flags, target)
            usleep(10_000)
        }
        try key(code, true, flags, target)
        usleep(25_000)
        try key(code, false, flags, target)
        usleep(10_000)
    } catch {
        release()
        throw error
    }
    release()
}

func combo(_ request: [String: Any]) throws -> [String: Any] {
    guard let text = request["combo"] as? String else { throw HelperError(code: "usage", message: "combo is missing") }
    let parts = text.lowercased().split(separator: "+").map(String.init)
    guard let last = parts.last, let code = keyCodes[last], parts.dropLast().allSatisfy({ modifierKeys[$0] != nil })
    else { throw HelperError(code: "usage", message: "Unknown key \(text)") }
    let (target, _) = try readyForInput()
    try press(code, holding: Array(parts.dropLast()), target)
    return ["ok": true, "input": foreground ? "foreground" : "background"]
}

func typeText(_ request: [String: Any]) throws -> [String: Any] {
    guard let text = request["text"] as? String, !text.isEmpty else {
        throw HelperError(code: "usage", message: "text is missing")
    }
    let paste = request["mode"] as? String == "paste"
    var strokes: [(CGKeyCode, Bool)] = []
    if !paste {
        for character in text {
            if character == "\n" { strokes.append((keyCodes["return"]!, false)); continue }
            let lower = String(character).lowercased()
            if let base = shifted[character], let code = keyCodes[base] {
                strokes.append((code, true))
            } else if let code = keyCodes[lower], lower.count == 1 {
                strokes.append((code, character.isUppercase))
            } else {
                throw HelperError(code: "usage", message: "This text needs a paste: it has characters with no key")
            }
        }
    }
    let (target, _) = try readyForInput()
    if paste {
        // The text stays on the Mac's pasteboard: putting the earlier contents back races the phone's read of it,
        // and the phone could paste those instead.
        let board = NSPasteboard.general
        board.clearContents()
        board.setString(text, forType: .string)
        guard board.string(forType: .string) == text else {
            throw HelperError(code: "usage", message: "The Mac's pasteboard didn't take the text; nothing was typed")
        }
        usleep(200_000)
        try press(keyCodes["v"]!, holding: ["cmd"], target)
    } else {
        for (code, shift) in strokes {
            try press(code, holding: shift ? ["shift"] : [], target)
            usleep(15_000)
        }
    }
    return ["ok": true, "input": foreground ? "foreground" : "background"]
}

// MARK: - the loop

func handle(_ request: [String: Any]) throws -> [String: Any] {
    // iPhone Mirroring itself came with macOS 15; the Mac app still runs on 13.
    guard #available(macOS 15.0, *) else {
        throw HelperError(code: "unsupported", message: "iPhone Mirroring needs macOS 15 or later")
    }
    foreground = request["input"] as? String == "foreground"
    switch request["op"] as? String ?? "" {
    case "state": return state()
    case "capture": return try capture(request)
    case "tap": return try tap(request)
    case "press": return try tap(request, holdMs: (request["ms"] as? Double) ?? 1500)
    case "drag": return try drag(request)
    case "scroll": return try scroll(request)
    case "key": return try combo(request)
    case "type": return try typeText(request)
    case "restore": return restoreFront()
    default: throw HelperError(code: "usage", message: "Unknown op")
    }
}

// MARK: - the live stream

// `mirroring stream FPS MAX_WIDTH QUALITY`: the Mirroring window as a stream of JPEG frames on standard output, each
// a 4-byte big-endian length and the JPEG, for the live view (mirroring.MirroringVideo). ScreenCaptureKit captures the
// window itself, so it streams while the window is behind other windows. A frame that shows no change is not sent.
// It ends when standard input closes (its parent went away) or the window does.
@available(macOS 14.0, *)
final class FrameStream: NSObject, SCStreamOutput, SCStreamDelegate {
    let quality: Double
    let out = FileHandle.standardOutput
    let context = CIContext(options: [.useSoftwareRenderer: false])
    var stream: SCStream?
    init(quality: Double) { self.quality = quality }

    func start(window: Window, fps: Double, maxWidth: Double) throws {
        let target = try scWindow(window.id)
        let filter = SCContentFilter(desktopIndependentWindow: target)
        let config = SCStreamConfiguration()
        let scale = min(Double(filter.pointPixelScale), maxWidth / max(window.frame.width, 1))
        config.width = Int(window.frame.width * scale)
        config.height = Int(window.frame.height * scale)
        config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(max(1, min(fps, 60))))
        config.showsCursor = false
        config.queueDepth = 3
        config.ignoreShadowsSingleWindow = true
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: DispatchQueue(label: "frames"))
        self.stream = stream
        stream.startCapture { error in
            if error != nil { exit(3) }
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, buffer.isValid,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: false)
                as? [[SCStreamFrameInfo: Any]],
              let raw = attachments.first?[.status] as? Int, SCFrameStatus(rawValue: raw) == .complete,
              let pixels = CMSampleBufferGetImageBuffer(buffer) else { return }
        let image = CIImage(cvPixelBuffer: pixels)
        guard let cg = context.createCGImage(image, from: image.extent)
        else { return }
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil)
        else { return }
        CGImageDestinationAddImage(destination, cg, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return }
        var length = UInt32(data.length).bigEndian
        out.write(Data(bytes: &length, count: 4))
        out.write(data as Data)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) { exit(4) }
}

if CommandLine.arguments.count >= 2 && CommandLine.arguments[1] == "stream" {
    let args = CommandLine.arguments
    let fps = args.count > 2 ? Double(args[2]) ?? 15 : 15
    let maxWidth = args.count > 3 ? Double(args[3]) ?? 640 : 640
    let quality = args.count > 4 ? Double(args[4]) ?? 0.6 : 0.6
    guard #available(macOS 15.0, *) else { exit(2) }
    guard let app = runningApp(), let window = phoneWindow(app) else { exit(5) }
    let frames = FrameStream(quality: quality)
    do { try frames.start(window: window, fps: fps, maxWidth: maxWidth) } catch { exit(6) }
    Thread.detachNewThread {
        while readLine() != nil {}
        exit(0)
    }
    dispatchMain()
}

// MARK: - files, by drag and drop (macOS 15.1+)

// `mirroring drop FX FY PATH...`: drags the files from a small Mobster window onto the phone at (FX, FY), fractions of
// the Mirroring window; the app in front on the phone receives them as a drop. `mirroring drag-out FX FY DEST`:
// long-presses the item at (FX, FY) on the phone, drags it onto a small Mobster window and saves what iOS hands over
// in DEST. Each prints one JSON line ({"ok":true,"sent":[...]} / {"ok":true,"received":[...]}, or {"ok":false,
// "code","error"}) and exits. The pointer is driven with HID events, so the Mirroring window must be in front.

final class DragSource: NSView, NSDraggingSource {
    var urls: [URL] = []
    var ended: ((NSDragOperation) -> Void)?
    var started = false
    override func mouseDown(with event: NSEvent) {}
    override func mouseDragged(with event: NSEvent) {
        if started { return }
        started = true
        let items = urls.map { url -> NSDraggingItem in
            let item = NSDraggingItem(pasteboardWriter: url as NSURL)
            item.setDraggingFrame(NSRect(x: 0, y: 0, width: 32, height: 32), contents: NSWorkspace.shared.icon(forFile: url.path))
            return item
        }
        beginDraggingSession(with: items, event: event, source: self)
    }
    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation {
        context == .outsideApplication ? [.copy, .generic] : []
    }
    func draggingSession(_ session: NSDraggingSession, endedAt point: NSPoint, operation: NSDragOperation) {
        ended?(operation)
    }
}

final class DropTarget: NSView {
    var destination = URL(fileURLWithPath: NSTemporaryDirectory())
    var finished: (([String], String?) -> Void)?
    override init(frame: NSRect) {
        super.init(frame: frame)
        registerForDraggedTypes(NSFilePromiseReceiver.readableDraggedTypes.map { NSPasteboard.PasteboardType($0) }
                                + [.fileURL, .URL, .string, .png, .tiff])
    }
    required init?(coder: NSCoder) { nil }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { .copy }
    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation { .copy }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let board = sender.draggingPasteboard
        if let promises = board.readObjects(forClasses: [NSFilePromiseReceiver.self]) as? [NSFilePromiseReceiver],
           !promises.isEmpty {
            var received: [String] = []
            let group = DispatchGroup()
            let queue = OperationQueue()
            for promise in promises {
                group.enter()
                promise.receivePromisedFiles(atDestination: destination, options: [:], operationQueue: queue) { url, error in
                    if error == nil { received.append(url.path) }
                    group.leave()
                }
            }
            group.notify(queue: .main) { self.finished?(received, received.isEmpty ? "refused" : nil) }
            return true
        }
        if let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL],
           !urls.isEmpty {
            var copied: [String] = []
            for url in urls {
                let target = destination.appendingPathComponent(url.lastPathComponent)
                if (try? FileManager.default.copyItem(at: url, to: target)) != nil { copied.append(target.path) }
            }
            finished?(copied, copied.isEmpty ? "refused" : nil)
            return true
        }
        if let image = NSImage(pasteboard: board), let tiff = image.tiffRepresentation,
           let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) {
            let target = destination.appendingPathComponent("from-iphone-\(Int(Date().timeIntervalSince1970)).png")
            if (try? png.write(to: target)) != nil {
                finished?([target.path], nil)
                return true
            }
        }
        if let text = board.string(forType: .string) {
            let target = destination.appendingPathComponent("from-iphone-\(Int(Date().timeIntervalSince1970)).txt")
            if (try? text.write(to: target, atomically: true, encoding: .utf8)) != nil {
                finished?([target.path], nil)
                return true
            }
        }
        finished?([], "refused")
        return false
    }
}

func finishTransfer(_ value: [String: Any]) -> Never {
    emit(value)
    exit(0)
}

// A small floating window of Mobster's at a global top-left point (CG coordinates), holding ``view``.
func transferWindow(at point: CGPoint, view: NSView) -> NSWindow {
    let screenHeight = NSScreen.screens.first?.frame.height ?? 900
    let size: CGFloat = 72
    let frame = NSRect(x: point.x - size / 2, y: screenHeight - point.y - size / 2, width: size, height: size)
    let window = NSWindow(contentRect: frame, styleMask: [.borderless], backing: .buffered, defer: false)
    window.level = .floating
    window.isOpaque = false
    window.backgroundColor = NSColor.systemPink.withAlphaComponent(0.25)
    view.frame = NSRect(x: 0, y: 0, width: size, height: size)
    window.contentView = view
    window.orderFrontRegardless()
    return window
}

func glide(_ from: CGPoint, _ to: CGPoint, steps: Int = 24, seconds: Double = 0.6) {
    for step in 1...steps {
        let t = Double(step) / Double(steps)
        postHID(.leftMouseDragged, CGPoint(x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t))
        usleep(useconds_t(seconds / Double(steps) * 1_000_000))
    }
}

func runTransfer(_ args: [String]) -> Never {
    guard #available(macOS 15.1, *) else {
        finishTransfer(["ok": false, "code": "unsupported", "error": "Dragging files to and from iPhone Mirroring needs macOS 15.1 or later"])
    }
    let mode = args[1]
    guard args.count >= 5, let fx = Double(args[2]), let fy = Double(args[3]), (0...1).contains(fx), (0...1).contains(fy)
    else { finishTransfer(["ok": false, "code": "usage", "error": "usage: drop FX FY PATH... | drag-out FX FY DEST"]) }
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    var mirror: Window
    do {
        foreground = true  // the drag needs the window in front and the Mac's pointer
        mirror = try readyForInput().target.window
    } catch let error as HelperError {
        finishTransfer(["ok": false, "code": error.code, "error": error.message])
    } catch {
        finishTransfer(["ok": false, "code": "capture", "error": "iPhone Mirroring isn't ready"])
    }
    let phonePoint = CGPoint(x: mirror.frame.minX + mirror.frame.width * fx, y: mirror.frame.minY + mirror.frame.height * fy)
    // Mobster's window sits just right of the Mirroring window (left of it when there's no room).
    let screenWidth = NSScreen.screens.first?.frame.width ?? 1440
    let side = mirror.frame.maxX + 60 < screenWidth ? mirror.frame.maxX + 50 : mirror.frame.minX - 50
    let macPoint = CGPoint(x: side, y: mirror.frame.midY)
    let deadline = DispatchTime.now() + 60
    DispatchQueue.main.asyncAfter(deadline: deadline) {
        finishTransfer(["ok": false, "code": "refused", "error": "Nothing came through in 60 s"])
    }
    if mode == "drop" {
        let paths = Array(args[4...])
        let urls = paths.map { URL(fileURLWithPath: $0) }
        for url in urls where !FileManager.default.fileExists(atPath: url.path) {
            finishTransfer(["ok": false, "code": "usage", "error": "No file at \(url.lastPathComponent)"])
        }
        let source = DragSource()
        source.urls = urls
        source.ended = { operation in
            finishTransfer(operation.isEmpty
                ? ["ok": false, "code": "refused", "error": "The app on the phone didn't take the drop"]
                : ["ok": true, "sent": urls.map { $0.lastPathComponent }])
        }
        let window = transferWindow(at: macPoint, view: source)
        _ = window
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) {
            postHID(.mouseMoved, macPoint)
            usleep(80_000)
            postHID(.leftMouseDown, macPoint)
            usleep(80_000)
            glide(macPoint, CGPoint(x: macPoint.x + 6, y: macPoint.y + 6), steps: 4, seconds: 0.1)
            glide(CGPoint(x: macPoint.x + 6, y: macPoint.y + 6), phonePoint)
            usleep(700_000)  // hover: the phone shows where the drop goes
            postHID(.leftMouseUp, phonePoint)
        }
    } else if mode == "drag-out" {
        let destination = URL(fileURLWithPath: args[4], isDirectory: true)
        try? FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
        let target = DropTarget(frame: .zero)
        target.destination = destination
        target.finished = { received, failure in
            finishTransfer(failure == nil ? ["ok": true, "received": received]
                           : ["ok": false, "code": failure!, "error": "Nothing came off the phone"])
        }
        let window = transferWindow(at: macPoint, view: target)
        _ = window
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) {
            postHID(.mouseMoved, phonePoint)
            usleep(80_000)
            postHID(.leftMouseDown, phonePoint)
            usleep(900_000)  // a long press lifts the item on iOS
            glide(phonePoint, CGPoint(x: phonePoint.x + 8, y: phonePoint.y + 8), steps: 6, seconds: 0.3)
            glide(CGPoint(x: phonePoint.x + 8, y: phonePoint.y + 8), macPoint, steps: 30, seconds: 0.9)
            usleep(600_000)
            postHID(.leftMouseUp, macPoint)
        }
    } else {
        finishTransfer(["ok": false, "code": "usage", "error": "Unknown transfer"])
    }
    app.run()
    exit(0)
}

if CommandLine.arguments.count >= 2 && ["drop", "drag-out"].contains(CommandLine.arguments[1]) {
    runTransfer(CommandLine.arguments)
}

// MARK: - the phone's sound (iPhone Mirroring plays it on the Mac)

// `mirroring audio --seconds N --out FILE [--live]`: records the iPhone Mirroring app's audio (ScreenCaptureKit) into
// FILE for N seconds (1 to 120), then prints {"ok":true,"recordedSeconds","peakDb","file"}. With --live it prints
// {"event":"recording"} once audio flows and {"event":"level","t","db"} every half second, and ends early when standard
// input reads "stop" or closes: phone-audio's live protocol, so mobile_agent/listen.py reads both the same way.
// Speech and sounds are told apart afterwards by phone-audio's transcribe and classify, on this Mac.
@available(macOS 14.0, *)
final class AudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let url: URL
    let live: Bool
    var file: AVAudioFile?
    var stream: SCStream?
    var started: Date?
    var peak: Float = -160
    var levelSum: Float = 0
    var levelCount = 0
    var lastLevel = Date.distantPast
    let lock = NSLock()
    init(url: URL, live: Bool) { self.url = url; self.live = live }

    func start(app: SCRunningApplication, display: SCDisplay) throws {
        let filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 1
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audio"))
        self.stream = stream
        stream.startCapture { error in
            if let error { emit(["ok": false, "error": "capture_failed", "message": "Mobster couldn't capture iPhone Mirroring's sound (\(type(of: error)))."]); exit(0) }
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer buffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, buffer.isValid, let description = buffer.formatDescription else { return }
        let format = AVAudioFormat(cmAudioFormatDescription: description)
        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(buffer))
        guard frames > 0, let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
        pcm.frameLength = frames
        guard CMSampleBufferCopyPCMDataIntoAudioBufferList(buffer, at: 0, frameCount: Int32(frames),
                                                           into: pcm.mutableAudioBufferList) == noErr else { return }
        lock.lock()
        defer { lock.unlock() }
        if file == nil {
            file = try? AVAudioFile(forWriting: url, settings: format.settings)
            started = Date()
            if live { emit(["event": "recording"]) }
        }
        try? file?.write(from: pcm)
        if let data = pcm.floatChannelData?[0] {
            var sum: Float = 0
            for i in 0..<Int(frames) { sum += data[i] * data[i] }
            let rms = sqrt(sum / Float(frames))
            let db = rms > 0 ? 20 * log10(rms) : -160
            peak = max(peak, db)
            levelSum += db
            levelCount += 1
        }
        if live, Date().timeIntervalSince(lastLevel) >= 0.5, levelCount > 0, let started {
            emit(["event": "level", "t": Date().timeIntervalSince(started), "db": Double(levelSum / Float(levelCount))])
            levelSum = 0
            levelCount = 0
            lastLevel = Date()
        }
    }

    func finish() -> Never {
        let done = DispatchSemaphore(value: 0)
        stream?.stopCapture { _ in done.signal() }
        _ = done.wait(timeout: .now() + 3)
        lock.lock()
        let seconds = started.map { Date().timeIntervalSince($0) } ?? 0
        file = nil  // closes the file
        lock.unlock()
        if seconds == 0 {
            emit(["ok": false, "error": "capture_failed", "message": "No sound came from iPhone Mirroring. Check that it's connected and the media is playing."])
        } else {
            emit(["ok": true, "recordedSeconds": seconds, "peakDb": Double(peak), "file": url.path])
        }
        exit(0)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) { finish() }
}

func runAudio(_ args: [String]) -> Never {
    func option(_ name: String) -> String? {
        guard let index = args.firstIndex(of: name), index + 1 < args.count else { return nil }
        return args[index + 1]
    }
    guard #available(macOS 15.0, *) else {
        emit(["ok": false, "error": "capture_failed", "message": "iPhone Mirroring needs macOS 15 or later."]); exit(0)
    }
    guard let out = option("--out") else {
        emit(["ok": false, "error": "usage", "message": "audio --seconds N --out FILE [--live]"]); exit(2)
    }
    let seconds = min(120, max(1, Double(option("--seconds") ?? "10") ?? 10))
    guard let running = runningApp() else {
        emit(["ok": false, "error": "no_device", "message": "iPhone Mirroring isn't open. Open it and connect your iPhone, then try again."]); exit(0)
    }
    let done = DispatchSemaphore(value: 0)
    let found = Box<(SCShareableContent?, Error?)>((nil, nil))
    SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, error in
        found.value = (content, error)
        done.signal()
    }
    _ = done.wait(timeout: .now() + 5)
    guard let content = found.value.0,
          let app = content.applications.first(where: { $0.processID == running.processIdentifier }),
          let display = content.displays.first else {
        emit(["ok": false, "error": "permission", "message": "Mobster needs Screen Recording to hear iPhone Mirroring. Allow it in System Settings › Privacy & Security › Screen & System Audio Recording."]); exit(0)
    }
    let recorder = AudioRecorder(url: URL(fileURLWithPath: out), live: args.contains("--live"))
    do { try recorder.start(app: app, display: display) } catch {
        emit(["ok": false, "error": "capture_failed", "message": "Mobster couldn't capture iPhone Mirroring's sound."]); exit(0)
    }
    Thread.detachNewThread {
        while let line = readLine() { if line.trimmingCharacters(in: .whitespaces) == "stop" { break } }
        if args.contains("--live") { recorder.finish() }
    }
    DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { recorder.finish() }
    dispatchMain()
}

if CommandLine.arguments.count >= 2 && CommandLine.arguments[1] == "audio" {
    runAudio(CommandLine.arguments)
}

while let line = readLine() {
    guard let data = line.data(using: .utf8),
          let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
        emit(["ok": false, "code": "usage", "error": "Each line must be a JSON object"])
        continue
    }
    do {
        var out = try handle(request)
        if let id = request["id"] { out["id"] = id }
        emit(out)
    } catch let error as HelperError {
        var out: [String: Any] = ["ok": false, "code": error.code, "error": error.message]
        if let id = request["id"] { out["id"] = id }
        emit(out)
    } catch {
        var out: [String: Any] = ["ok": false, "code": "capture", "error": "\(type(of: error))"]
        if let id = request["id"] { out["id"] = id }
        emit(out)
    }
}
