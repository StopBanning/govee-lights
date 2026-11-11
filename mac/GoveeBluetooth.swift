// Govee Bluetooth: carries Govee's 20-byte light commands to Bluetooth-only lights (like the H617A strip).
// It knows nothing about colors or brightness: govee/govee_ble.py builds the packets and reads the replies.
// It's an app, not a bare program, so macOS asks once whether "Govee Bluetooth" may use Bluetooth, whoever runs it.
//
//   scan <seconds>                  one JSON line per Govee light heard: {"id", "name", "rssi"}
//   send <id> <hex>... [--read]     connect, write each packet; with --read, also print each light reply as hex
//   serve <socket path>             stay running and connected, taking the commands above over a Unix socket
//
// Python runs it through `open -W -n <app> --args --out <file> ...`, since `open` can't hand back output or an exit
// code. So every line goes to that file, ending with "exit <code>" and, on failure, "error <reason>". Codes: 0 done,
// 2 bad arguments, 3 Bluetooth off or not allowed, 4 light not found, 5 couldn't connect or write. Without --out it
// prints to the terminal instead.
import CoreBluetooth
import Foundation

let service = CBUUID(string: "00010203-0405-0607-0809-0A0B0C0D1910")
let writeChar = CBUUID(string: "00010203-0405-0607-0809-0A0B0C0D2B11")
let notifyChar = CBUUID(string: "00010203-0405-0607-0809-0A0B0C0D2B10")

let output: FileHandle = {
    let args = CommandLine.arguments
    if let flag = args.firstIndex(of: "--out"), flag + 1 < args.count {
        FileManager.default.createFile(atPath: args[flag + 1], contents: nil)
        if let file = FileHandle(forWritingAtPath: args[flag + 1]) { return file }
    }
    return FileHandle.standardOutput
}()

func say(_ line: String) {
    output.write(Data((line + "\n").utf8))
}

func finish(_ code: Int32, _ problem: String = "") -> Never {
    if !problem.isEmpty {
        say("error " + problem)
    }
    say("exit \(code)")
    exit(code)
}

func isGovee(_ name: String) -> Bool {
    name.hasPrefix("Govee_") || name.hasPrefix("GVH") || name.hasPrefix("ihoment_") || name.hasPrefix("Minger_")
}

func bytes(_ hex: String) -> Data? {
    guard hex.count % 2 == 0 else { return nil }
    var data = Data()
    var index = hex.startIndex
    while index < hex.endIndex {
        let next = hex.index(index, offsetBy: 2)
        guard let byte = UInt8(hex[index..<next], radix: 16) else { return nil }
        data.append(byte)
        index = next
    }
    return data
}

final class Link: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    let command: String
    let target: UUID?
    let packets: [Data]
    let read: Bool
    let seconds: Double
    var central: CBCentralManager!
    var light: CBPeripheral?
    var writer: CBCharacteristic?
    var seen = Set<UUID>()
    var replies = 0

    init(arguments: [String]) {
        command = arguments.first ?? ""
        read = arguments.contains("--read")
        let rest = Array(arguments.dropFirst()).filter { $0 != "--read" }
        if command == "scan" {
            target = nil
            packets = []
            seconds = Double(rest.first ?? "") ?? 4
        } else if command == "send", let id = rest.first.flatMap(UUID.init(uuidString:)) {
            target = id
            let decoded = rest.dropFirst().map(bytes)
            if decoded.isEmpty || decoded.contains(where: { $0 == nil }) {
                finish(2, "send needs a light id and one or more hex packets")
            }
            packets = decoded.compactMap { $0 }
            seconds = 10
        } else {
            finish(2, "usage: scan <seconds> | send <id> <hex>... [--read]")
        }
        super.init()
        central = CBCentralManager(delegate: self, queue: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [self] in
            if command == "scan" { finish(0) }
            finish(light == nil ? 4 : 5, light == nil ? "the light wasn't found" : "the light didn't answer in time")
        }
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        switch central.state {
        case .poweredOn:
            if let target = target, let known = central.retrievePeripherals(withIdentifiers: [target]).first {
                connect(known)
            } else {
                central.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
            }
        case .unauthorized:
            finish(3, "Bluetooth isn't allowed for Govee Bluetooth: System Settings > Privacy & Security > Bluetooth")
        case .poweredOff:
            finish(3, "Bluetooth is off")
        case .unsupported:
            finish(3, "this Mac has no Bluetooth")
        default:
            break
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi: NSNumber) {
        let name = (advertisementData[CBAdvertisementDataLocalNameKey] as? String) ?? peripheral.name ?? ""
        if command == "scan" {
            guard isGovee(name), !seen.contains(peripheral.identifier) else { return }
            seen.insert(peripheral.identifier)
            let entry: [String: Any] = ["id": peripheral.identifier.uuidString, "name": name, "rssi": rssi.intValue]
            if let json = try? JSONSerialization.data(withJSONObject: entry) {
                say(String(decoding: json, as: UTF8.self))
            }
        } else if peripheral.identifier == target {
            central.stopScan()
            connect(peripheral)
        }
    }

    func connect(_ peripheral: CBPeripheral) {
        light = peripheral
        peripheral.delegate = self
        central.connect(peripheral)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        peripheral.discoverServices([service])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        finish(5, "couldn't connect: \(error?.localizedDescription ?? "no reason given"). Is the Govee app connected to it?")
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard let found = peripheral.services?.first(where: { $0.uuid == service }) else {
            finish(5, "this light doesn't offer Govee's Bluetooth service")
        }
        peripheral.discoverCharacteristics([writeChar, notifyChar], for: found)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        let chars = service.characteristics ?? []
        guard let writer = chars.first(where: { $0.uuid == writeChar }) else {
            finish(5, "this light has no Govee command channel")
        }
        self.writer = writer
        if read, let notify = chars.first(where: { $0.uuid == notifyChar }) {
            peripheral.setNotifyValue(true, for: notify)  // then writes start in didUpdateNotificationState
        } else {
            writeAll(peripheral)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic,
                    error: Error?) {
        writeAll(peripheral)
    }

    func writeAll(_ peripheral: CBPeripheral) {
        guard let writer = writer else { return }
        let type: CBCharacteristicWriteType = writer.properties.contains(.writeWithoutResponse)
            ? .withoutResponse : .withResponse
        for (index, packet) in packets.enumerated() {
            // A small gap between packets: Govee lights drop some that arrive back to back.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.06 * Double(index)) {
                peripheral.writeValue(packet, for: writer, type: type)
            }
        }
        // Without --read, finish once the last packet has had time to go out. With it, wait for the replies.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.06 * Double(packets.count) + (read ? 1.5 : 0.3)) {
            self.central.cancelPeripheralConnection(peripheral)
            finish(0)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard let value = characteristic.value else { return }
        say(value.map { String(format: "%02x", $0) }.joined())
    }
}

// MARK: - Staying connected

// `serve <socket path>` keeps running and keeps lights connected between commands, so a change takes a fraction of
// a second instead of the 2-3 a fresh connection costs. Python sends one JSON line per connection on the Unix
// socket, {"args": ["send", ...]} or {"args": ["scan", "3"]}, and gets back {"lines": [...]}: the same lines a
// one-shot run writes, ending with "exit <code>". A light is let go after a minute without commands, so the Govee
// phone app can reach it; the server quits after ten quiet minutes, and Python starts it again when needed.
final class Connection {
    let peripheral: CBPeripheral
    var writer: CBCharacteristic?
    var notify: CBCharacteristic?
    var ready = false
    var waiting: [(String?) -> Void] = []  // run once ready (nil) or failed (the reason)
    var lastUsed = Date()

    init(_ peripheral: CBPeripheral) {
        self.peripheral = peripheral
    }
}

final class Server: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    let path: String
    var central: CBCentralManager!
    var powered = false
    var connections: [UUID: Connection] = [:]
    var heard: [UUID: CBPeripheral] = [:]  // seen while scanning, so they can be connected by id
    var scanning: (until: Date, found: [String], seen: Set<UUID>)?
    var collecting: [String]?  // light replies during a send with --read
    var queue: [([String], ([String]) -> Void)] = []
    var busy = false
    var lastRequest = Date()

    init(path: String) {
        self.path = path
        super.init()
        central = CBCentralManager(delegate: self, queue: nil)
        listen()
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.tidy() }
    }

    // A thread accepts connections; each request is handled on the main queue, one at a time.
    func listen() {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8.prefix(MemoryLayout.size(ofValue: address.sun_path) - 1))
        withUnsafeMutableBytes(of: &address.sun_path) { buffer in
            buffer.copyBytes(from: bytes)
        }
        // Another copy already serving? Then this one leaves: two would fight over the same lights.
        let probe = socket(AF_UNIX, SOCK_STREAM, 0)
        let alive = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(probe, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        close(probe)
        if alive == 0 { exit(0) }
        unlink(path)
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard fd >= 0, bound == 0, Darwin.listen(fd, 8) == 0 else {
            FileHandle.standardError.write(Data("can't listen on \(path)\n".utf8))
            exit(1)
        }
        chmod(path, 0o600)
        Thread {
            while true {
                let client = accept(fd, nil, nil)
                if client < 0 { continue }
                Thread { self.serve(client) }.start()
            }
        }.start()
    }

    func serve(_ client: Int32) {
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while !data.contains(10), data.count < 65536 {
            let count = read(client, &buffer, buffer.count)
            if count <= 0 { break }
            data.append(contentsOf: buffer[0..<count])
        }
        let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let args = request?["args"] as? [String] ?? []
        let done = DispatchSemaphore(value: 0)
        var lines: [String] = []
        DispatchQueue.main.async {
            self.enqueue(args) { result in
                lines = result
                done.signal()
            }
        }
        done.wait()
        if let reply = try? JSONSerialization.data(withJSONObject: ["lines": lines]) {
            reply.withUnsafeBytes { _ = write(client, $0.baseAddress, reply.count) }
            _ = write(client, "\n", 1)
        }
        close(client)
    }

    func enqueue(_ args: [String], _ done: @escaping ([String]) -> Void) {
        queue.append((args, done))
        next()
    }

    func next() {
        guard !busy, powered || central.state != .unknown, !queue.isEmpty else { return }
        let (args, done) = queue.removeFirst()
        busy = true
        lastRequest = Date()
        var finished = false
        let finish: ([String]) -> Void = { lines in
            if finished { return }
            finished = true
            self.busy = false
            done(lines)
            self.next()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) { finish(["error the light didn't answer in time", "exit 5"]) }
        guard powered else {
            finish(["error Bluetooth is off or not allowed for Govee Bluetooth", "exit 3"])
            return
        }
        switch args.first {
        case "scan":
            startScan(seconds: Double(args.dropFirst().first ?? "") ?? 3, done: finish)
        case "send":
            let read = args.contains("--read")
            let rest = Array(args.dropFirst()).filter { $0 != "--read" }
            guard let id = rest.first.flatMap(UUID.init(uuidString:)) else {
                finish(["error send needs a light id", "exit 2"])
                return
            }
            let decoded = rest.dropFirst().map(bytes)
            guard !decoded.isEmpty, !decoded.contains(where: { $0 == nil }) else {
                finish(["error send needs hex packets", "exit 2"])
                return
            }
            send(id, packets: decoded.compactMap { $0 }, read: read, done: finish)
        default:
            finish(["error unknown command", "exit 2"])
        }
    }

    func startScan(seconds: Double, done: @escaping ([String]) -> Void) {
        scanning = (Date().addingTimeInterval(seconds), [], [])
        central.scanForPeripherals(withServices: nil, options: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) {
            self.central.stopScan()
            let found = self.scanning?.found ?? []
            self.scanning = nil
            done(found + ["exit 0"])
        }
    }

    func send(_ id: UUID, packets: [Data], read: Bool, done: @escaping ([String]) -> Void) {
        ready(id) { problem in
            guard problem == nil, let connection = self.connections[id], let writer = connection.writer else {
                done(["error " + (problem ?? "the light has no Govee command channel"), problem == "the light wasn't found" ? "exit 4" : "exit 5"])
                return
            }
            connection.lastUsed = Date()
            if read { self.collecting = [] }
            let type: CBCharacteristicWriteType = writer.properties.contains(.writeWithoutResponse) ? .withoutResponse : .withResponse
            for (index, packet) in packets.enumerated() {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05 * Double(index)) {
                    connection.peripheral.writeValue(packet, for: writer, type: type)
                }
            }
            let after = 0.05 * Double(packets.count)
            if !read {
                DispatchQueue.main.asyncAfter(deadline: .now() + after + 0.05) { done(["exit 0"]) }
                return
            }
            // Wait for the replies: two "aa" answers, or about a second at most.
            func check(_ tries: Int) {
                let replies = self.collecting ?? []
                if replies.filter({ $0.hasPrefix("aa") }).count >= 2 || tries == 0 {
                    self.collecting = nil
                    done(replies + ["exit 0"])
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { check(tries - 1) }
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + after) { check(24) }
        }
    }

    // Connects (or finds the light first) and calls back once it can take commands.
    func ready(_ id: UUID, _ then: @escaping (String?) -> Void) {
        if let connection = connections[id], connection.ready, connection.peripheral.state == .connected {
            then(nil)
            return
        }
        let peripheral = connections[id]?.peripheral ?? heard[id] ?? central.retrievePeripherals(withIdentifiers: [id]).first
        guard let found = peripheral else {
            // Not known yet: listen for it briefly.
            startScan(seconds: 4) { _ in
                if self.heard[id] != nil { self.ready(id, then) } else { then("the light wasn't found") }
            }
            return
        }
        let connection = connections[id] ?? Connection(found)
        connections[id] = connection
        connection.waiting.append(then)
        if found.state != .connecting {  // a hung attempt mustn't leave later commands waiting forever
            found.delegate = self
            central.connect(found)
        }
    }

    func settle(_ peripheral: CBPeripheral, _ problem: String?) {
        guard let connection = connections[peripheral.identifier] else { return }
        connection.ready = problem == nil
        let waiting = connection.waiting
        connection.waiting = []
        waiting.forEach { $0(problem) }
    }

    func tidy() {
        for connection in connections.values where Date().timeIntervalSince(connection.lastUsed) > 60 {
            central.cancelPeripheralConnection(connection.peripheral)  // let the Govee app reach it
            connection.ready = false
        }
        if Date().timeIntervalSince(lastRequest) > 600 && !busy {
            unlink(path)
            exit(0)
        }
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        powered = central.state == .poweredOn
        next()
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi: NSNumber) {
        let name = (advertisementData[CBAdvertisementDataLocalNameKey] as? String) ?? peripheral.name ?? ""
        guard isGovee(name) else { return }
        heard[peripheral.identifier] = peripheral
        if var scan = scanning, !scan.seen.contains(peripheral.identifier) {
            scan.seen.insert(peripheral.identifier)
            let entry: [String: Any] = ["id": peripheral.identifier.uuidString, "name": name, "rssi": rssi.intValue]
            if let json = try? JSONSerialization.data(withJSONObject: entry) {
                scan.found.append(String(decoding: json, as: UTF8.self))
            }
            scanning = scan
        }
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        peripheral.discoverServices([service])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        settle(peripheral, "couldn't connect: \(error?.localizedDescription ?? "no reason given"). Is the Govee app connected to it?")
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        connections[peripheral.identifier]?.ready = false
        settle(peripheral, "the light disconnected")
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard let found = peripheral.services?.first(where: { $0.uuid == service }) else {
            settle(peripheral, "this light doesn't offer Govee's Bluetooth service")
            return
        }
        peripheral.discoverCharacteristics([writeChar, notifyChar], for: found)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard let connection = connections[peripheral.identifier] else { return }
        let chars = service.characteristics ?? []
        connection.writer = chars.first { $0.uuid == writeChar }
        connection.notify = chars.first { $0.uuid == notifyChar }
        if let notify = connection.notify {
            peripheral.setNotifyValue(true, for: notify)  // ready once replies are on
        } else {
            settle(peripheral, connection.writer == nil ? "this light has no Govee command channel" : nil)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic,
                    error: Error?) {
        settle(peripheral, connections[peripheral.identifier]?.writer == nil ? "this light has no Govee command channel" : nil)
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard let value = characteristic.value, collecting != nil else { return }
        collecting?.append(value.map { String(format: "%02x", $0) }.joined())
    }
}

var arguments = Array(CommandLine.arguments.dropFirst())
if let flag = arguments.firstIndex(of: "--out") {
    arguments.removeSubrange(flag..<min(flag + 2, arguments.count))
}
if arguments.first == "serve", arguments.count >= 2 {
    let server = Server(path: arguments[1])
    withExtendedLifetime(server) { RunLoop.main.run() }
} else {
    let link = Link(arguments: arguments)
    withExtendedLifetime(link) { RunLoop.main.run() }
}
