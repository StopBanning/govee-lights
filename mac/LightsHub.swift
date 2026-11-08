// Lights Hub: keeps the lights hub (../govee/lights_page.py) running under this app's name. macOS then asks once
// whether "Lights Hub" may reach devices on the local network, instead of quietly blocking a bare Python.
import Foundation

let repo = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
let script = repo.appendingPathComponent("govee/lights_page.py").path
let python = FileManager.default.isExecutableFile(atPath: "/opt/homebrew/bin/python3")
    ? "/opt/homebrew/bin/python3" : "/usr/bin/python3"

let hub = Process()
hub.executableURL = URL(fileURLWithPath: python)
hub.arguments = [script]
hub.terminationHandler = { exit($0.terminationStatus) }  // launchd starts us again if the hub stops

// When launchd stops us, stop the hub too.
var signalSources: [DispatchSourceSignal] = []
for number in [SIGTERM, SIGINT] {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler { hub.terminate() }
    source.resume()
    signalSources.append(source)
}

do {
    try hub.run()
} catch {
    FileHandle.standardError.write(Data("Lights Hub: couldn't start \(python) \(script): \(error)\n".utf8))
    exit(1)
}
dispatchMain()
