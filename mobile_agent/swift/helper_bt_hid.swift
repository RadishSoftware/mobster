// bt-hid: this Mac as a Bluetooth LE keyboard and mouse for iPhones, for Mobster's "Cable + Bluetooth" connection
// (mobile_agent/cable_bt.py). Experimental: used only with MOBSTER_EXPERIMENTAL_CABLE_BT=1.
//
// How: a CoreBluetooth peripheral that publishes the HID over GATT profile (HID service 0x1812, Battery 0x180F,
// Device Information 0x180A). iOS binds it as a keyboard and a mouse, and AssistiveTouch turns the mouse into touches.
// Classic Bluetooth HID isn't possible here: macOS's bluetoothd owns the HID L2CAP channels and is always the host.
// The standard UUIDs are written in their 128-bit form, because CoreBluetooth refuses the 16-bit ones as reserved.
// One Mac identity serves several iPhones at once: each one is a central subscribed to the reports, and every report
// goes to exactly one of them.
//
// One long-lived process for the whole Mac, spoken to in JSON lines on stdin, or with --socket PATH on a Unix socket
// that every Mobster process on this Mac shares (one per phone; it ends 2 minutes after its last client leaves).
// Requests are handled side by side; each request {"op", "id", ...} gets one
// answer line {"id", "ok": true, ...} or {"id", "ok": false, "code", "error"}. Lines with "event" and no id report
// hosts subscribing and leaving.
//
//   {"op":"status"}   {"state","authorization","advertising","hosts":[{"host","subscribed","ready"}]}
//   {"op":"advertise","seconds":N}   Advertise as "Mobster Keyboard" for N seconds (at most 600; 0 stops), so the
//                                    iPhone's Settings > Bluetooth lists it under Other Devices.
//   {"op":"move","host","dx","dy","step":8,"interval_ms":15,"mask":0}
//       Relative pointer motion in HID counts, as reports of at most `step` counts each, `interval_ms` apart (a
//       constant pace keeps AssistiveTouch's pointer acceleration constant). mask holds buttons during the motion.
//   {"op":"buttons","host","mask"}   Mouse buttons held now: bit 0 primary, 1 secondary, 2 middle.
//   {"op":"click","host","ms":60,"button":1}
//   {"op":"key","host","combo":"cmd+space"}   Press and release (modifiers cmd, shift, alt, ctrl).
//   {"op":"type","host","text","interval_ms":12}  US key codes only: anything else is refused before a key is sent.
//   {"op":"release","host"}          Every key and button up.
//
// Failures: no_bluetooth (off or no controller), permission (macOS denies Bluetooth to the app that started this),
// publish (the HID service wasn't accepted), no_host (that iPhone isn't subscribed), refused (an input this helper
// won't send), write (a report wasn't taken in time: the outcome is unknown and the caller must not retry), usage.
// Nothing is retried. A failed report ends the request and leaves the rest unsent.

import CoreBluetooth
import Foundation

/// Where lines go: stdout (fd 1) in the plain mode, and each connected client in --socket mode. Events go to all.
final class Outputs {
    let lock = NSLock()
    var fds: Set<Int32> = []

    func write(_ line: String, to fd: Int32) {
        lock.lock()
        defer { lock.unlock() }
        var bytes = Array((line + "\n").utf8)
        var offset = 0
        while offset < bytes.count {
            let n = bytes.withUnsafeMutableBytes { Darwin.write(fd, $0.baseAddress! + offset, $0.count - offset) }
            if n <= 0 { return }
            offset += n
        }
    }
}

let outputs = Outputs()

func encode(_ value: [String: Any]) -> String? {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return nil }
    return String(data: data, encoding: .utf8)
}

/// An answer to one requester, or (fd nil) an event to every listener.
func emit(_ value: [String: Any], to fd: Int32? = nil) {
    guard let line = encode(value) else { return }
    if let fd = fd {
        outputs.write(line, to: fd)
        return
    }
    outputs.lock.lock()
    let all = outputs.fds
    outputs.lock.unlock()
    for target in all { outputs.write(line, to: target) }
}

struct Refusal: Error {
    let code: String
    let message: String
}

/// A Bluetooth SIG UUID in its 128-bit form.
func sig(_ short: UInt16) -> CBUUID {
    CBUUID(string: String(format: "%08X-0000-1000-8000-00805F9B34FB", UInt32(short)))
}

// MARK: - Reports

let REPORT_KEYBOARD: UInt8 = 1
let REPORT_MOUSE: UInt8 = 2
let REPORT_CONSUMER: UInt8 = 3

/// A keyboard (report 1, with its LED output), a three-button mouse with 16-bit relative X and Y and a wheel
/// (report 2), and one consumer control (report 3).
let REPORT_MAP: [UInt8] = [
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, REPORT_KEYBOARD,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
    0xC0,
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, REPORT_MOUSE, 0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x03, 0x15, 0x00, 0x25, 0x01, 0x95, 0x03, 0x75, 0x01, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x05, 0x81, 0x01,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x16, 0x01, 0x80, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02, 0x81, 0x06,
    0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
    0xC0, 0xC0,
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, REPORT_CONSUMER,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
]

let EMPTY: [UInt8: Data] = [REPORT_KEYBOARD: Data(count: 8), REPORT_MOUSE: Data(count: 6), REPORT_CONSUMER: Data(count: 2)]

// MARK: - Keys (US layout)

let MODIFIERS: [String: UInt8] = ["ctrl": 0x01, "control": 0x01, "shift": 0x02, "alt": 0x04, "option": 0x04,
                                  "cmd": 0x08, "command": 0x08]
let NAMED: [String: UInt8] = [
    "return": 0x28, "enter": 0x28, "escape": 0x29, "esc": 0x29, "backspace": 0x2A, "delete": 0x2A, "tab": 0x2B,
    "space": 0x2C, "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "home": 0x4A, "end": 0x4D,
    "pageup": 0x4B, "pagedown": 0x4E,
]
let SHIFTED: [Character: (UInt8, Bool)] = [
    "!": (0x1E, true), "@": (0x1F, true), "#": (0x20, true), "$": (0x21, true), "%": (0x22, true),
    "^": (0x23, true), "&": (0x24, true), "*": (0x25, true), "(": (0x26, true), ")": (0x27, true),
    "-": (0x2D, false), "_": (0x2D, true), "=": (0x2E, false), "+": (0x2E, true), "[": (0x2F, false),
    "{": (0x2F, true), "]": (0x30, false), "}": (0x30, true), "\\": (0x31, false), "|": (0x31, true),
    ";": (0x33, false), ":": (0x33, true), "'": (0x34, false), "\"": (0x34, true), "`": (0x35, false),
    "~": (0x35, true), ",": (0x36, false), "<": (0x36, true), ".": (0x37, false), ">": (0x37, true),
    "/": (0x38, false), "?": (0x38, true),
]

/// The usage and the shift a character needs on a US keyboard, or nil.
func usage(of char: Character) -> (UInt8, Bool)? {
    guard char.unicodeScalars.count == 1, let scalar = char.unicodeScalars.first, scalar.isASCII else { return nil }
    let v = scalar.value
    switch v {
    case 0x61...0x7A: return (UInt8(0x04 + v - 0x61), false)
    case 0x41...0x5A: return (UInt8(0x04 + v - 0x41), true)
    case 0x31...0x39: return (UInt8(0x1E + v - 0x31), false)
    case 0x30: return (0x27, false)
    case 0x0A: return (0x28, false)
    case 0x20: return (0x2C, false)
    default: return SHIFTED[char]
    }
}

func parseCombo(_ combo: String) throws -> (UInt8, UInt8) {
    var mods: UInt8 = 0
    var key: UInt8 = 0
    for part in combo.lowercased().split(separator: "+").map(String.init) {
        if let m = MODIFIERS[part] {
            mods |= m
        } else if let k = NAMED[part] {
            key = k
        } else if part.count == 1, let (k, shift) = usage(of: Character(part)) {
            key = k
            if shift { mods |= 0x02 }
        } else {
            throw Refusal(code: "usage", message: "Unknown key \(part)")
        }
    }
    return (mods, key)
}

// MARK: - The peripheral

final class Keyboard: NSObject, CBPeripheralManagerDelegate {
    let queue = DispatchQueue(label: "bt-hid.bluetooth")
    var manager: CBPeripheralManager!
    var inputs: [UInt8: CBMutableCharacteristic] = [:]
    var published = 0
    var publishError: String?
    /// Subscribed iPhones: central identifier -> (central, report ids it subscribed to).
    var hosts: [String: (CBCentral, Set<UInt8>)] = [:]
    let ready = DispatchSemaphore(value: 0)
    var advertisingUntil: Date?

    override init() {
        super.init()
        manager = CBPeripheralManager(delegate: self, queue: queue)
    }

    var stateName: String {
        switch manager.state {
        case .poweredOn: return "on"
        case .poweredOff: return "off"
        case .unauthorized: return "unauthorized"
        case .unsupported: return "unsupported"
        case .resetting: return "resetting"
        default: return "unknown"
        }
    }

    static func authorization() -> String {
        switch CBManager.authorization {
        case .allowedAlways: return "allowed"
        case .denied: return "denied"
        case .restricted: return "restricted"
        default: return "not_determined"
        }
    }

    func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        emit(["event": "state", "state": stateName, "authorization": Keyboard.authorization()])
        if peripheral.state == .poweredOn && inputs.isEmpty { publish() }
    }

    func publish() {
        let encrypted: CBAttributePermissions = [.readEncryptionRequired, .writeEncryptionRequired]
        func characteristic(_ uuid: UInt16, _ properties: CBCharacteristicProperties,
                            _ reference: (UInt8, UInt8)? = nil) -> CBMutableCharacteristic {
            let made = CBMutableCharacteristic(type: sig(uuid), properties: properties, value: nil,
                                               permissions: encrypted)
            if let (id, kind) = reference {
                made.descriptors = [CBMutableDescriptor(type: sig(0x2908), value: Data([id, kind]))]
            }
            return made
        }
        let hid = CBMutableService(type: sig(0x1812), primary: true)
        var chars: [CBMutableCharacteristic] = [
            characteristic(0x2A4A, [.read]),                                  // HID Information
            characteristic(0x2A4B, [.read]),                                  // Report Map
            characteristic(0x2A4C, [.writeWithoutResponse]),                  // HID Control Point
            characteristic(0x2A4E, [.read, .writeWithoutResponse]),           // Protocol Mode
        ]
        for id in [REPORT_KEYBOARD, REPORT_MOUSE, REPORT_CONSUMER] {
            let input = characteristic(0x2A4D, [.read, .notify], (id, 1))
            inputs[id] = input
            chars.append(input)
        }
        chars.append(characteristic(0x2A4D, [.read, .write, .writeWithoutResponse], (REPORT_KEYBOARD, 2)))  // LEDs
        hid.characteristics = chars
        let battery = CBMutableService(type: sig(0x180F), primary: true)
        battery.characteristics = [characteristic(0x2A19, [.read, .notify])]
        let info = CBMutableService(type: sig(0x180A), primary: true)
        info.characteristics = [characteristic(0x2A29, [.read]), characteristic(0x2A50, [.read])]
        for service in [info, battery, hid] { manager.add(service) }
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didAdd service: CBService, error: Error?) {
        if let error = error {
            publishError = "\(service.uuid): \(error.localizedDescription)"
            emit(["event": "publish", "ok": false, "service": service.uuid.uuidString,
                  "error": error.localizedDescription])
        } else {
            published += 1
            emit(["event": "publish", "ok": true, "service": service.uuid.uuidString])
        }
    }

    func value(for uuid: CBUUID, _ characteristic: CBCharacteristic) -> Data? {
        switch uuid {
        case sig(0x2A4A): return Data([0x11, 0x01, 0x00, 0x02])            // HID 1.11, no country, connectable
        case sig(0x2A4B): return Data(REPORT_MAP)
        case sig(0x2A4E): return Data([0x01])                               // report protocol
        case sig(0x2A19): return Data([100])
        case sig(0x2A29): return Data("Mobster".utf8)
        case sig(0x2A50): return Data([0x02, 0xAC, 0x05, 0x01, 0x00, 0x01, 0x00])  // PnP: USB, ids, version
        case sig(0x2A4D):
            if let id = inputs.first(where: { $0.value === characteristic })?.key { return EMPTY[id] }
            return Data([0])
        default: return nil
        }
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveRead request: CBATTRequest) {
        guard let data = value(for: request.characteristic.uuid, request.characteristic) else {
            peripheral.respond(to: request, withResult: .attributeNotFound)
            return
        }
        guard request.offset <= data.count else {
            peripheral.respond(to: request, withResult: .invalidOffset)
            return
        }
        request.value = data.subdata(in: request.offset..<data.count)
        peripheral.respond(to: request, withResult: .success)
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, didReceiveWrite requests: [CBATTRequest]) {
        if let first = requests.first { peripheral.respond(to: first, withResult: .success) }
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral,
                           didSubscribeTo characteristic: CBCharacteristic) {
        guard let id = inputs.first(where: { $0.value === characteristic })?.key else { return }
        let key = central.identifier.uuidString
        var entry = hosts[key] ?? (central, [])
        entry.1.insert(id)
        hosts[key] = entry
        peripheral.setDesiredConnectionLatency(.low, for: central)
        // A host may wait for a first report after subscribing.
        _ = peripheral.updateValue(EMPTY[id]!, for: inputs[id]!, onSubscribedCentrals: [central])
        emit(["event": "host", "host": key, "state": "subscribed", "report": Int(id), "ready": isReady(entry.1)])
    }

    func peripheralManager(_ peripheral: CBPeripheralManager, central: CBCentral,
                           didUnsubscribeFrom characteristic: CBCharacteristic) {
        guard let id = inputs.first(where: { $0.value === characteristic })?.key else { return }
        let key = central.identifier.uuidString
        hosts[key]?.1.remove(id)
        if hosts[key]?.1.isEmpty == true { hosts[key] = nil }
        emit(["event": "host", "host": key, "state": "unsubscribed", "report": Int(id)])
    }

    func peripheralManagerIsReady(toUpdateSubscribers peripheral: CBPeripheralManager) {
        ready.signal()
    }

    func isReady(_ reports: Set<UInt8>) -> Bool { reports.contains(REPORT_MOUSE) && reports.contains(REPORT_KEYBOARD) }

    // MARK: requests (called off the Bluetooth queue)

    func onQueue<T>(_ body: () throws -> T) rethrows -> T { try queue.sync(execute: body) }

    func host(_ request: [String: Any]) throws -> CBCentral {
        try onQueue {
            let key = (request["host"] as? String ?? "").uppercased()
            if let (central, reports) = hosts[key], isReady(reports) { return central }
            let ready = hosts.values.filter { isReady($0.1) }
            if key.isEmpty, ready.count == 1 { return ready[0].0 }
            throw Refusal(code: "no_host", message: key.isEmpty
                ? "\(ready.count) iPhones use this Mac as a keyboard now; name one"
                : "That iPhone isn't using this Mac as a keyboard now")
        }
    }

    /// One input report to one iPhone. Waits up to a second for room in CoreBluetooth's queue, then fails as write.
    func send(_ central: CBCentral, _ id: UInt8, _ payload: [UInt8]) throws {
        let deadline = Date().addingTimeInterval(1)
        while true {
            let taken = onQueue { () -> Bool in
                guard let characteristic = inputs[id] else { return false }
                return manager.updateValue(Data(payload), for: characteristic, onSubscribedCentrals: [central])
            }
            if taken { return }
            if ready.wait(timeout: .now() + max(deadline.timeIntervalSinceNow, 0)) == .timedOut {
                throw Refusal(code: "write", message: "The iPhone didn't take a report in 1 s; its outcome is unknown")
            }
        }
    }

    func mouse(_ c: CBCentral, _ buttons: UInt8, _ dx: Int, _ dy: Int) throws {
        let x = UInt16(bitPattern: Int16(max(-32767, min(32767, dx))))
        let y = UInt16(bitPattern: Int16(max(-32767, min(32767, dy))))
        try send(c, REPORT_MOUSE, [buttons & 0x07, UInt8(x & 0xFF), UInt8(x >> 8), UInt8(y & 0xFF), UInt8(y >> 8), 0])
    }

    func key(_ c: CBCentral, mods: UInt8, key: UInt8, hold: Double) throws {
        try send(c, REPORT_KEYBOARD, [mods, 0, key, 0, 0, 0, 0, 0])
        Thread.sleep(forTimeInterval: hold)
        try send(c, REPORT_KEYBOARD, [0, 0, 0, 0, 0, 0, 0, 0])
    }

    func handle(_ request: [String: Any]) throws -> [String: Any] {
        func num(_ k: String, _ d: Double) -> Double { (request[k] as? NSNumber)?.doubleValue ?? d }
        let op = request["op"] as? String ?? ""
        if op != "status" {
            let state = onQueue { manager.state }
            if state == .unauthorized {
                throw Refusal(code: "permission", message: "macOS doesn't let Mobster use Bluetooth")
            }
            if state != .poweredOn { throw Refusal(code: "no_bluetooth", message: "Bluetooth is off on this Mac") }
            if let error = onQueue({ publishError }) { throw Refusal(code: "publish", message: error) }
        }
        switch op {
        case "status":
            return onQueue {
                ["state": stateName, "authorization": Keyboard.authorization(), "published": published,
                 "advertising": manager.isAdvertising,
                 "hosts": hosts.map { ["host": $0.key, "subscribed": $0.value.1.map { Int($0) }.sorted(),
                                       "ready": isReady($0.value.1)] }]
            }
        case "advertise":
            let seconds = min(max(num("seconds", 120), 0), 600)
            onQueue {
                manager.stopAdvertising()
                if seconds > 0 {
                    manager.startAdvertising([CBAdvertisementDataLocalNameKey: "Mobster Keyboard",
                                              CBAdvertisementDataServiceUUIDsKey: [sig(0x1812)]])
                }
            }
            if seconds > 0 {
                queue.asyncAfter(deadline: .now() + seconds) { [weak self] in self?.manager.stopAdvertising() }
            }
            return ["seconds": seconds]
        case "move":
            let c = try host(request)
            var left = (Int(num("dx", 0)), Int(num("dy", 0)))
            let step = max(1, min(Int(num("step", 8)), 32767))
            let interval = num("interval_ms", 15) / 1000
            let buttons = UInt8(num("mask", 0))
            var sent = 0
            while left.0 != 0 || left.1 != 0 {
                let x = max(-step, min(step, left.0)), y = max(-step, min(step, left.1))
                try mouse(c, buttons, x, y)
                left = (left.0 - x, left.1 - y)
                sent += 1
                if interval > 0 { Thread.sleep(forTimeInterval: interval) }
            }
            return ["reports": sent]
        case "buttons":
            try mouse(try host(request), UInt8(num("mask", 0)), 0, 0)
            return [:]
        case "click":
            let c = try host(request)
            let bit = UInt8(1 << (max(1, min(Int(num("button", 1)), 3)) - 1))
            try mouse(c, bit, 0, 0)
            Thread.sleep(forTimeInterval: num("ms", 60) / 1000)
            try mouse(c, 0, 0, 0)
            return [:]
        case "key":
            let (mods, k) = try parseCombo(request["combo"] as? String ?? "")
            try key(try host(request), mods: mods, key: k, hold: 0.04)
            return [:]
        case "type":
            let text = request["text"] as? String ?? ""
            var keys: [(UInt8, Bool)] = []
            for char in text {
                guard let k = usage(of: char) else {
                    throw Refusal(code: "refused", message: "This keyboard can't type \(String(char).debugDescription)")
                }
                keys.append(k)
            }
            let c = try host(request)
            let interval = num("interval_ms", 12) / 1000
            for (k, shift) in keys {
                try key(c, mods: shift ? 0x02 : 0, key: k, hold: interval)
                Thread.sleep(forTimeInterval: interval)
            }
            return ["typed": keys.count]
        case "release":
            let c = try host(request)
            try send(c, REPORT_KEYBOARD, [0, 0, 0, 0, 0, 0, 0, 0])
            try mouse(c, 0, 0, 0)
            try send(c, REPORT_CONSUMER, [0, 0])
            return [:]
        default:
            throw Refusal(code: "usage", message: "Unknown op \(op)")
        }
    }
}

// MARK: - Main

// A client that leaves mid-answer must not end the helper for every other phone: writes to it just fail.
signal(SIGPIPE, SIG_IGN)
let keyboard = Keyboard()

/// Reads requests from `fd` until it closes; each runs side by side (one iPhone's long move never holds up another's
/// tap) and its answer goes back to `fd`. Each phone's driver sends one request at a time and waits for its answer.
func serve(_ fd: Int32, group: DispatchGroup) {
    guard let stream = fdopen(fd, "r") else { return }
    var buffer: UnsafeMutablePointer<CChar>? = nil
    var capacity = 0
    while getline(&buffer, &capacity, stream) > 0 {
        let line = String(cString: buffer!)
        guard let data = line.data(using: .utf8),
              let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            emit(["ok": false, "code": "usage", "error": "Not a JSON object"], to: fd == 0 ? 1 : fd)
            continue
        }
        let id = request["id"] ?? NSNull()
        let reply: Int32 = fd == 0 ? 1 : fd
        DispatchQueue.global(qos: .userInitiated).async(group: group) {
            do {
                var answer = try keyboard.handle(request)
                answer["id"] = id
                answer["ok"] = true
                emit(answer, to: reply)
            } catch let refusal as Refusal {
                emit(["id": id, "ok": false, "code": refusal.code, "error": refusal.message], to: reply)
            } catch {
                emit(["id": id, "ok": false, "code": "write", "error": "\(error)"], to: reply)
            }
        }
    }
    free(buffer)
    group.wait()
}

let arguments = Array(CommandLine.arguments.dropFirst())
if let index = arguments.firstIndex(of: "--socket"), index + 1 < arguments.count {
    // Shared mode: one keyboard for every Mobster process on this Mac (one per phone), at a Unix socket only this
    // user can reach. It ends IDLE_SECONDS after its last client leaves.
    let path = arguments[index + 1]
    let IDLE_SECONDS = 120.0
    unlink(path)
    let server = socket(AF_UNIX, SOCK_STREAM, 0)
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    let capacity = MemoryLayout.size(ofValue: address.sun_path)
    guard path.utf8.count < capacity else { emit(["event": "ready", "ok": false, "error": "socket path too long"], to: 1); exit(2) }
    withUnsafeMutableBytes(of: &address.sun_path) { raw in
        for (i, byte) in path.utf8.enumerated() { raw[i] = byte }
    }
    let old = umask(0o077)
    let bound = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(server, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    umask(old)
    guard bound == 0, listen(server, 16) == 0 else {
        emit(["event": "ready", "ok": false, "error": "couldn't listen at \(path)"], to: 1)
        exit(1)
    }
    emit(["event": "ready", "ok": true, "socket": path], to: 1)
    let clients = NSLock()
    var count = 0
    var lastLeft = Date()
    Thread.detachNewThread {
        while true {
            Thread.sleep(forTimeInterval: 5)
            clients.lock()
            let idle = count == 0 && Date().timeIntervalSince(lastLeft) > IDLE_SECONDS
            clients.unlock()
            if idle { unlink(path); exit(0) }
        }
    }
    while true {
        let fd = accept(server, nil, nil)
        if fd < 0 { continue }
        clients.lock(); count += 1; clients.unlock()
        outputs.lock.lock(); outputs.fds.insert(fd); outputs.lock.unlock()
        Thread.detachNewThread {
            serve(fd, group: DispatchGroup())
            outputs.lock.lock(); outputs.fds.remove(fd); outputs.lock.unlock()
            close(fd)
            clients.lock(); count -= 1; lastLeft = Date(); clients.unlock()
        }
    }
} else {
    outputs.fds.insert(1)
    serve(0, group: DispatchGroup())
}
