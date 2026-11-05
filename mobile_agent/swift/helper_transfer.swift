// transfer: hands files from this Mac to an iPhone without a cable, for mobile_agent/phone_io/handoff.py.
//
//   transfer airdrop --to NAME [--timeout S] FILE...
//       AirDrop's own panel (NSSharingService), and a press on the one recipient whose name is exactly NAME. Another
//       name, two devices with that name, or none in time: Cancel is pressed and nothing is sent.
//       {"ok":true,"sent":N,"seconds":S} | {"ok":false,"code":"not_found"|"ambiguous","seen":[names]}
//   transfer drop --x FX --y FY FILE...
//       Drags the files from a 32-point spot of this helper's onto the iPhone Mirroring window, at a point given as
//       fractions of the window, as a person drags from Finder. iPhone Mirroring hands them to the app in front on
//       the phone. {"ok":true,"operation":N} once the window took them.
//
// One JSON line on standard output. Errors: {"ok":false,"code","error"}: "usage", "permission" (Accessibility is
// missing), "no_window" (iPhone Mirroring isn't showing the phone), "refused" (the window didn't take the drop),
// "failed" (AirDrop said it failed or the person on the phone declined), "timeout". Nothing is retried here.
//
// The Mac app carries it as Contents/Resources/transfer; a source checkout builds it with
// native_helpers.build("transfer").

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

let mirroringBundle = "com.apple.ScreenContinuity"

func emit(_ value: [String: Any]) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data("{}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(value["ok"] as? Bool == true ? 0 : 1)
}

func fail(_ code: String, _ message: String, _ extra: [String: Any] = [:]) -> Never {
    var out: [String: Any] = ["ok": false, "code": code, "error": message]
    for (key, value) in extra { out[key] = value }
    emit(out)
}

// MARK: - arguments

var arguments = Array(CommandLine.arguments.dropFirst())
guard let op = arguments.first, ["airdrop", "drop"].contains(op) else {
    fail("usage", "transfer airdrop --to NAME FILE... | transfer drop --x FX --y FY FILE...")
}
arguments.removeFirst()
var options: [String: String] = [:]
var paths: [String] = []
var index = 0
while index < arguments.count {
    let word = arguments[index]
    if word.hasPrefix("--"), index + 1 < arguments.count {
        options[String(word.dropFirst(2))] = arguments[index + 1]
        index += 2
    } else {
        paths.append(word)
        index += 1
    }
}
let files = paths.map { URL(fileURLWithPath: $0) }
if files.isEmpty || files.contains(where: { !FileManager.default.isReadableFile(atPath: $0.path) }) {
    fail("usage", "Name files that exist and can be read")
}
if !AXIsProcessTrusted() {
    fail("permission", "Mobster needs Accessibility to do this. Turn it on in System Settings › Privacy & Security › Accessibility.")
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

// MARK: - AirDrop

func attribute(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: AnyObject?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

/// The AirDrop panel's recipients ("dd.nodeBody" buttons, named by their description) and its Cancel button.
func panel() -> (recipients: [(name: String, element: AXUIElement)], cancel: AXUIElement?) {
    var recipients: [(String, AXUIElement)] = []
    var cancel: AXUIElement?
    func walk(_ node: AXUIElement, _ depth: Int) {
        if depth > 16 { return }
        let role = attribute(node, kAXRoleAttribute) as? String ?? ""
        let name = attribute(node, kAXDescriptionAttribute) as? String ?? ""
        let identifier = attribute(node, kAXIdentifierAttribute) as? String ?? ""
        if role == kAXButtonRole {
            if identifier == "dd.nodeBody", !name.isEmpty { recipients.append((name, node)) }
            if identifier.isEmpty, name == "Cancel" { cancel = node }
        }
        for child in (attribute(node, kAXChildrenAttribute) as? [AXUIElement]) ?? [] { walk(child, depth + 1) }
    }
    walk(AXUIElementCreateApplication(getpid()), 0)
    return (recipients, cancel)
}

final class Sharing: NSObject, NSSharingServiceDelegate {
    let started = Date()
    func sharingService(_ sharingService: NSSharingService, didShareItems items: [Any]) {
        emit(["ok": true, "sent": items.count, "seconds": (Date().timeIntervalSince(started) * 10).rounded() / 10])
    }
    func sharingService(_ sharingService: NSSharingService, didFailToShareItems items: [Any], error: Error) {
        let cancelled = (error as NSError).code == NSUserCancelledError
        fail(cancelled ? "cancelled" : "failed", cancelled ? "The AirDrop was cancelled" : error.localizedDescription)
    }
}

func airdrop() {
    guard let recipient = options["to"], !recipient.isEmpty else { fail("usage", "Name the iPhone with --to") }
    let waitForRecipient = Double(options["timeout"] ?? "30") ?? 30
    let waitForTransfer = Double(options["transfer-timeout"] ?? "900") ?? 900
    guard let service = NSSharingService(named: .sendViaAirDrop), service.canPerform(withItems: files) else {
        fail("failed", "AirDrop isn't available on this Mac. Turn on Wi-Fi and Bluetooth.")
    }
    let delegate = Sharing()
    service.delegate = delegate
    service.perform(withItems: files)
    var pressed = false
    var seen: Set<String> = []
    let opened = Date()
    Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { timer in
        let (recipients, cancel) = panel()
        seen.formUnion(recipients.map(\.name))
        let matching = recipients.filter { $0.name == recipient }
        if !pressed && matching.count > 1 {
            if let cancel { AXUIElementPerformAction(cancel, kAXPressAction as CFString) }
            fail("ambiguous", "More than one device nearby is called \(recipient)", ["seen": Array(seen).sorted()])
        }
        if !pressed, let only = matching.first {
            // Wait one more beat: a second device with the same name can show up a moment later.
            if Date().timeIntervalSince(opened) < 2.5 { return }
            pressed = AXUIElementPerformAction(only.element, kAXPressAction as CFString) == .success
            if !pressed { fail("failed", "AirDrop didn't take the press on \(recipient)") }
            timer.invalidate()
            DispatchQueue.main.asyncAfter(deadline: .now() + waitForTransfer) {
                fail("timeout", "AirDrop didn't finish within \(Int(waitForTransfer)) seconds")
            }
            return
        }
        if Date().timeIntervalSince(opened) > waitForRecipient {
            if let cancel { AXUIElementPerformAction(cancel, kAXPressAction as CFString) }
            fail("not_found", "AirDrop didn't show \(recipient) within \(Int(waitForRecipient)) seconds",
                 ["seen": Array(seen).sorted()])
        }
    }
}

// MARK: - a drop on iPhone Mirroring

func post(_ type: CGEventType, _ point: CGPoint) {
    CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
}

final class DragSource: NSView, NSDraggingSource {
    var finished: ((NSDragOperation) -> Void)?
    func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation {
        .copy
    }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) {
        let items = files.map { url -> NSDraggingItem in
            let item = NSDraggingItem(pasteboardWriter: url as NSURL)
            item.setDraggingFrame(bounds, contents: NSWorkspace.shared.icon(forFile: url.path))
            return item
        }
        beginDraggingSession(with: items, event: event, source: self)
    }
    func draggingSession(_ session: NSDraggingSession, endedAt screenPoint: NSPoint, operation: NSDragOperation) {
        finished?(operation)
    }
}

func mirroringWindow() -> (app: NSRunningApplication, frame: CGRect)? {
    guard let running = NSRunningApplication.runningApplications(withBundleIdentifier: mirroringBundle).first else {
        return nil
    }
    let windows = (CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
        as? [[String: Any]]) ?? []
    for window in windows where (window[kCGWindowOwnerPID as String] as? pid_t) == running.processIdentifier
        && (window[kCGWindowLayer as String] as? Int) == 0 {
        guard let bounds = window[kCGWindowBounds as String] as? [String: Double] else { continue }
        let frame = CGRect(x: bounds["X"] ?? 0, y: bounds["Y"] ?? 0, width: bounds["Width"] ?? 0,
                           height: bounds["Height"] ?? 0)
        if frame.width > 150, frame.height > 300 { return (running, frame) }
    }
    return nil
}

func drop() {
    guard let fx = Double(options["x"] ?? ""), let fy = Double(options["y"] ?? ""), (0...1).contains(fx),
          (0...1).contains(fy) else {
        fail("usage", "--x and --y are fractions of the window, 0 to 1")
    }
    guard let (mirroring, frame) = mirroringWindow() else {
        fail("no_window", "iPhone Mirroring isn't showing the phone")
    }
    let front = NSWorkspace.shared.frontmostApplication
    mirroring.activate()
    let target = CGPoint(x: frame.minX + frame.width * fx, y: frame.minY + frame.height * fy)
    // The drag starts beside the window (left of it, or right when it sits at the screen's left edge).
    let screen = NSScreen.screens.first?.frame ?? CGRect(x: 0, y: 0, width: 1440, height: 900)
    let startX = frame.minX > 80 ? frame.minX - 48 : min(frame.maxX + 48, screen.width - 24)
    let start = CGPoint(x: startX, y: target.y)
    let window = NSWindow(contentRect: NSRect(x: start.x - 16, y: screen.height - start.y - 16, width: 32, height: 32),
                          styleMask: .borderless, backing: .buffered, defer: false)
    window.level = .screenSaver
    window.isOpaque = false
    window.backgroundColor = NSColor.white.withAlphaComponent(0.02)
    let source = DragSource(frame: NSRect(x: 0, y: 0, width: 32, height: 32))
    window.contentView = source
    window.orderFrontRegardless()
    let saved = CGEvent(source: nil)?.location
    func finish(_ value: [String: Any]) -> Never {
        if let saved { CGWarpMouseCursorPosition(saved) }
        if let front, front.bundleIdentifier != mirroringBundle { front.activate() }
        emit(value)
    }
    source.finished = { operation in
        if operation.isEmpty {
            finish(["ok": false, "code": "refused", "error": "iPhone Mirroring didn't take the files"])
        }
        finish(["ok": true, "operation": Int(operation.rawValue)])
    }
    DispatchQueue.global().asyncAfter(deadline: .now() + 0.4) {
        post(.mouseMoved, start)
        usleep(60_000)
        post(.leftMouseDown, start)
        usleep(120_000)
        for step in 1...30 {
            let t = Double(step) / 30
            post(.leftMouseDragged, CGPoint(x: start.x + (target.x - start.x) * t, y: start.y + (target.y - start.y) * t))
            usleep(20_000)
        }
        // Held over the window a moment so it can say it takes them, then let go.
        for _ in 0..<12 {
            post(.leftMouseDragged, target)
            usleep(50_000)
        }
        post(.leftMouseUp, target)
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 15) {
        finish(["ok": false, "code": "timeout", "error": "The drag didn't finish"])
    }
}

DispatchQueue.main.async { op == "airdrop" ? airdrop() : drop() }
app.run()
