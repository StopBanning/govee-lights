// Govee Bluetooth: carries Govee's 20-byte light commands to Bluetooth-only lights (like the H617A strip).
// It knows nothing about colors or brightness: govee/govee_ble.py builds the packets and reads the replies.
// It's an app, not a bare program, so macOS asks once whether "Govee Bluetooth" may use Bluetooth, whoever runs it.
//
//   scan <seconds>                  one JSON line per Govee light heard: {"id", "name", "rssi"}
//   send <id> <hex>... [--read]     connect, write each packet; with --read, also print each light reply as hex
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

var arguments = Array(CommandLine.arguments.dropFirst())
if let flag = arguments.firstIndex(of: "--out") {
    arguments.removeSubrange(flag..<min(flag + 2, arguments.count))
}
let link = Link(arguments: arguments)
withExtendedLifetime(link) { RunLoop.main.run() }
