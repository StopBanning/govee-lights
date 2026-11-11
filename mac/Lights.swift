// Lights: a menu bar button for the Govee lights. It talks to the lights hub (Lights Hub.app) on port 8737.
import AppKit

let hub = URL(string: "http://127.0.0.1:8737")!
let scenes = [("Warm", "warm"), ("Bright", "bright"), ("Dim", "dim"), ("On", "on"), ("Off", "off")]
// The hub's named colors (govee_lan.COLORS), for the color row.
let swatches: [(name: String, red: CGFloat, green: CGFloat, blue: CGFloat)] = [
    ("red", 255, 0, 0), ("orange", 255, 100, 0), ("yellow", 255, 190, 0), ("green", 0, 255, 0),
    ("teal", 0, 200, 140), ("blue", 0, 0, 255), ("purple", 130, 0, 255), ("pink", 255, 50, 140),
]

// A color dot in the menu that knows which lights and which color it's for ("" opens the color picker).
final class ColorButton: NSButton {
    var lights = "all"
    var colorName = ""
}

// A slider that knows which lights and which setting it's for ("brightness" or "temperature"), and where to show
// its value. Temperature moves in 100K steps.
final class LightSlider: NSSlider {
    var lights = "all"
    var setting = "brightness"
    weak var valueLabel: NSTextField?

    var level: Int {
        setting == "temperature" ? Int((doubleValue / 100).rounded()) * 100 : Int(doubleValue.rounded())
    }

    var shown: String {
        setting == "temperature" ? "\(level)K" : "\(level)%"
    }
}

@MainActor
final class LightsMenu: NSObject, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private var summary = "Asking the lights…"
    private var groups: [String] = []
    private var lights: [[String: Any]] = []  // the hub's last answer, where the sliders start from
    private var symbol = "lightbulb"
    private var commonScenes: [String] = []  // Govee scenes every kind of light has
    private var sceneCategories: [String: [(name: String, scenes: [String])]] = [:]  // each kind's, in Govee's groups
    private var pickerLights = "all"  // which lights the color picker is choosing for
    private var pickerTimer: Timer?
    private var followingSpotify = false  // the hub's "Follow Spotify" switch
    private var spotifyNote = ""  // the song it's following, or why it can't

    override init() {
        super.init()
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        show()
        refresh()
        refreshScenes()
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            DispatchQueue.main.async { self?.refresh() }
        }
    }

    // Each time the menu opens: rebuild it from the last state, and fetch fresh state for the icon.
    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let header = NSMenuItem(title: summary, action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(.separator())
        for (title, scene) in scenes {
            menu.addItem(sceneItem(title, scene: scene, target: "all"))
        }
        for slider in sliderItems(target: "all") {
            menu.addItem(slider)
        }
        menu.addItem(colorItem(target: "all"))
        if !commonScenes.isEmpty {
            menu.addItem(goveeScenesItem(target: "all"))
        }
        if !groups.isEmpty {
            menu.addItem(.separator())
        }
        for group in groups {
            let parent = NSMenuItem(title: group.prefix(1).uppercased() + group.dropFirst(), action: nil, keyEquivalent: "")
            let submenu = NSMenu()
            for (title, scene) in scenes {
                submenu.addItem(sceneItem(title, scene: scene, target: group))
            }
            submenu.addItem(.separator())
            for slider in sliderItems(target: group) {
                submenu.addItem(slider)
            }
            submenu.addItem(colorItem(target: group))
            if !commonScenes.isEmpty {
                submenu.addItem(goveeScenesItem(target: group))
            }
            parent.submenu = submenu
            menu.addItem(parent)
        }
        if commonScenes.isEmpty {
            refreshScenes()
        }
        menu.addItem(.separator())
        let follow = NSMenuItem(title: "Follow Spotify", action: #selector(toggleSpotify), keyEquivalent: "")
        follow.target = self
        follow.state = followingSpotify ? .on : .off
        follow.toolTip = "The lights take each song's album-art colors while Spotify plays."
        menu.addItem(follow)
        if followingSpotify && !spotifyNote.isEmpty {
            let note = NSMenuItem(title: spotifyNote, action: nil, keyEquivalent: "")
            note.isEnabled = false
            menu.addItem(note)
        }
        menu.addItem(.separator())
        let page = NSMenuItem(title: "Open the Lights Page", action: #selector(openPage), keyEquivalent: "")
        page.target = self
        menu.addItem(page)
        menu.addItem(NSMenuItem(title: "Quit Lights", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        refresh()
    }

    private func sceneItem(_ title: String, scene: String, target: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: #selector(runScene(_:)), keyEquivalent: "")
        item.target = self
        item.representedObject = [scene, target]
        return item
    }

    // Brightness (1–100%) and white temperature (2700–6500K, what both models do), like Govee's sliders.
    private func sliderItems(target: String) -> [NSMenuItem] {
        let kelvin = min(6500, max(2700, (average("colorTemInKelvin", target, fallback: 4000) + 50) / 100 * 100))
        return [sliderItem("Brightness", setting: "brightness", value: average("brightness", target, fallback: 50),
                           range: 1...100, target: target),
                sliderItem("White temperature", setting: "temperature", value: kelvin, range: 2700...6500, target: target)]
    }

    // A slider in the menu. It sends its value once, when it's let go.
    private func sliderItem(_ title: String, setting: String, value: Int, range: ClosedRange<Double>,
                            target: String) -> NSMenuItem {
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 240, height: 46))
        let label = NSTextField(labelWithString: title)
        label.frame = NSRect(x: 20, y: 26, width: 140, height: 16)
        label.textColor = .secondaryLabelColor
        let slider = LightSlider(value: Double(value), minValue: range.lowerBound, maxValue: range.upperBound,
                                 target: self, action: #selector(sliderMoved(_:)))
        slider.frame = NSRect(x: 18, y: 4, width: 204, height: 22)
        slider.isContinuous = true
        slider.lights = target
        slider.setting = setting
        let shown = NSTextField(labelWithString: slider.shown)
        shown.frame = NSRect(x: 160, y: 26, width: 60, height: 16)
        shown.alignment = .right
        shown.textColor = .secondaryLabelColor
        slider.valueLabel = shown
        for subview in [label, shown, slider] as [NSView] {
            view.addSubview(subview)
        }
        let item = NSMenuItem()
        item.view = view
        return item
    }

    // A row of color dots, like the phone page's: one click sets that color. The last dot opens the color picker.
    private func colorItem(target: String) -> NSMenuItem {
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 240, height: 52))
        let label = NSTextField(labelWithString: "Color")
        label.frame = NSRect(x: 20, y: 32, width: 140, height: 16)
        label.textColor = .secondaryLabelColor
        view.addSubview(label)
        for (index, swatch) in (swatches.map { Optional($0) } + [nil]).enumerated() {
            let button = ColorButton(frame: NSRect(x: 18 + index * 23, y: 6, width: 20, height: 20))
            button.lights = target
            button.title = ""
            button.isBordered = false
            button.wantsLayer = true
            button.layer?.cornerRadius = 10
            button.target = self
            if let swatch = swatch {
                button.colorName = swatch.name
                button.layer?.backgroundColor = NSColor(srgbRed: swatch.red / 255, green: swatch.green / 255,
                                                        blue: swatch.blue / 255, alpha: 1).cgColor
                button.toolTip = swatch.name.capitalized
                button.action = #selector(colorPicked(_:))
            } else {
                button.image = NSImage(systemSymbolName: "eyedropper", accessibilityDescription: "Any color")
                button.toolTip = "Any color…"
                button.action = #selector(openColorPicker(_:))
            }
            view.addSubview(button)
        }
        let item = NSMenuItem()
        item.view = view
        return item
    }

    // The average of a state value over the chosen lights that are on. Kelvin is 0 while a light shows a color,
    // so zeros don't count.
    private func average(_ key: String, _ target: String, fallback: Int) -> Int {
        let values = lights.compactMap { light -> Int? in
            guard let state = light["state"] as? [String: Any], state["onOff"] as? Int == 1 else { return nil }
            let kind = (light["kind"] as? String ?? "") + "s"
            guard target == "all" || kind == target || light["room"] as? String == target else { return nil }
            guard let value = state[key] as? Int, value > 0 else { return nil }
            return value
        }
        return values.isEmpty ? fallback : values.reduce(0, +) / values.count
    }

    // One kind: its scenes in Govee's groups. All lights or a room: the shared scenes, then each kind's own,
    // which start on just that kind.
    private func goveeScenesItem(target: String) -> NSMenuItem {
        let parent = NSMenuItem(title: "Govee Scenes", action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        if let own = sceneCategories[target] {
            for category in own {
                submenu.addItem(categoryItem(category.name, category.scenes, target: target))
            }
        } else {
            for name in commonScenes {
                submenu.addItem(goveeSceneItem(name, target: target))
            }
            let shared = Set(commonScenes)
            for kind in sceneCategories.keys.sorted() {
                let extra = (sceneCategories[kind] ?? []).compactMap { category -> (name: String, scenes: [String])? in
                    let names = category.scenes.filter { !shared.contains($0) }
                    return names.isEmpty ? nil : (name: category.name, scenes: names)
                }
                if extra.isEmpty {
                    continue
                }
                submenu.addItem(.separator())
                let only = NSMenuItem(title: kind.prefix(1).uppercased() + kind.dropFirst() + " only", action: nil,
                                      keyEquivalent: "")
                let onlyMenu = NSMenu()
                for category in extra {
                    onlyMenu.addItem(categoryItem(category.name, category.scenes, target: kind))
                }
                only.submenu = onlyMenu
                submenu.addItem(only)
            }
        }
        parent.submenu = submenu
        return parent
    }

    private func categoryItem(_ name: String, _ names: [String], target: String) -> NSMenuItem {
        let item = NSMenuItem(title: name, action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        for scene in names {
            submenu.addItem(goveeSceneItem(scene, target: target))
        }
        item.submenu = submenu
        return item
    }

    private func goveeSceneItem(_ name: String, target: String) -> NSMenuItem {
        let item = NSMenuItem(title: name, action: #selector(runGoveeScene(_:)), keyEquivalent: "")
        item.target = self
        item.representedObject = [name, target]
        return item
    }

    @objc private func runScene(_ sender: NSMenuItem) {
        guard let pick = sender.representedObject as? [String], pick.count == 2 else { return }
        var parts = URLComponents(url: hub.appendingPathComponent("api/scene/\(pick[0])"), resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "target", value: pick[1])]
        var request = URLRequest(url: parts.url!, timeoutInterval: 20)
        request.httpMethod = "POST"
        perform(request)
    }

    @objc private func runGoveeScene(_ sender: NSMenuItem) {
        guard let pick = sender.representedObject as? [String], pick.count == 2 else { return }
        postJSON("api/govee-scene", ["name": pick[0], "target": pick[1]], timeout: 30)
    }

    @objc private func toggleSpotify() {
        followingSpotify.toggle()
        var request = URLRequest(url: hub.appendingPathComponent("api/spotify"), timeoutInterval: 10)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["enabled": followingSpotify])
        call(request) { reply in
            self.followingSpotify = reply?["enabled"] as? Bool ?? self.followingSpotify
        }
    }

    // A color, scene or slider from the menu ends following on the hub; mirror that here.
    private func stopFollowing() {
        followingSpotify = false
    }

    @objc private func colorPicked(_ sender: ColorButton) {
        statusItem.menu?.cancelTracking()
        postJSON("api/set", ["target": sender.lights, "color": sender.colorName], timeout: 20)
    }

    @objc private func openColorPicker(_ sender: ColorButton) {
        statusItem.menu?.cancelTracking()
        pickerLights = sender.lights
        let panel = NSColorPanel.shared
        panel.showsAlpha = false
        panel.isContinuous = true
        panel.setTarget(self)
        panel.setAction(#selector(pickerChanged(_:)))
        NSApp.activate()
        panel.makeKeyAndOrderFront(nil)
    }

    // The picker reports every step of a drag, so wait until the color settles, then send it once.
    @objc private func pickerChanged(_ panel: NSColorPanel) {
        guard let color = panel.color.usingColorSpace(.sRGB) else { return }
        let hex = String(format: "#%02x%02x%02x", Int((color.redComponent * 255).rounded()),
                         Int((color.greenComponent * 255).rounded()), Int((color.blueComponent * 255).rounded()))
        let lights = pickerLights
        pickerTimer?.invalidate()
        pickerTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: false) { [weak self] _ in
            DispatchQueue.main.async { self?.postJSON("api/set", ["target": lights, "color": hex], timeout: 20) }
        }
    }

    @objc private func sliderMoved(_ slider: LightSlider) {
        slider.valueLabel?.stringValue = slider.shown
        guard NSApp.currentEvent?.type == .leftMouseUp else { return }  // send once, when it's let go
        postJSON("api/set", ["target": slider.lights, slider.setting: slider.level], timeout: 20)
    }

    private func postJSON(_ path: String, _ body: [String: Any], timeout: TimeInterval) {
        var request = URLRequest(url: hub.appendingPathComponent(path), timeoutInterval: timeout)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        perform(request)
    }

    // Sends a change to the hub. A full success refreshes the icon; anything else shows the hub's words.
    private func perform(_ request: URLRequest) {
        stopFollowing()
        symbol = "ellipsis.circle"
        show()
        call(request) { reply in
            if reply?["ok"] as? Bool == true {
                self.refresh()
            } else {
                self.summary = reply?["text"] as? String ?? "The lights hub didn't answer."
                self.symbol = "exclamationmark.triangle"
                self.show()
            }
        }
    }

    private func refresh() {
        refreshSpotify()
        call(URLRequest(url: hub.appendingPathComponent("api/lights"), timeoutInterval: 10)) { reply in
            guard let reply = reply, let lights = reply["lights"] as? [[String: Any]] else {
                self.summary = reply?["error"] as? String ?? "The lights hub isn't running."
                self.symbol = "lightbulb.slash"
                self.show()
                return
            }
            let states = lights.map { $0["state"] as? [String: Any] }
            let on = states.filter { $0?["onOff"] as? Int == 1 }.count
            let silent = states.filter { $0 == nil }.count
            self.lights = lights
            if silent > 0 && silent == lights.count {
                // Not "0 of 4 on": they may well be on. They just didn't answer.
                self.summary = "None of the \(lights.count) lights answered. Run: python3 govee/govee_lan.py doctor"
            } else {
                self.summary = "\(on) of \(lights.count) on" + (silent > 0 ? " · \(silent) not answering" : "")
            }
            self.groups = (reply["groups"] as? [String] ?? []).filter { $0 != "all" }
            self.symbol = silent > 0 && silent == lights.count ? "lightbulb.slash" : (on > 0 ? "lightbulb.fill" : "lightbulb")
            self.show()
        }
    }

    private func refreshSpotify() {
        call(URLRequest(url: hub.appendingPathComponent("api/spotify"), timeoutInterval: 10)) { reply in
            guard let reply = reply else { return }
            self.followingSpotify = reply["enabled"] as? Bool ?? false
            let track = reply["track"] as? [String: Any] ?? [:]
            if let problem = reply["problem"] as? String, !problem.isEmpty {
                self.spotifyNote = "Can't reach Spotify: allow Lights Hub in Automation settings"
            } else if let name = track["name"] as? String, let artist = track["artist"] as? String {
                self.spotifyNote = "♪ \(name) – \(artist)"
            } else {
                self.spotifyNote = "Waiting for Spotify to play"
            }
        }
    }

    private func refreshScenes() {
        call(URLRequest(url: hub.appendingPathComponent("api/scenes"), timeoutInterval: 30)) { reply in
            guard let common = reply?["common"] as? [String] else { return }
            var categories: [String: [(name: String, scenes: [String])]] = [:]
            for (kind, list) in reply?["categories"] as? [String: [[String: Any]]] ?? [:] {
                categories[kind] = list.compactMap { entry -> (name: String, scenes: [String])? in
                    guard let name = entry["name"] as? String, let names = entry["scenes"] as? [String] else { return nil }
                    return (name: name, scenes: names)
                }
            }
            self.commonScenes = common
            self.sceneCategories = categories
        }
    }

    // Runs a request and hands its JSON reply (or nil) to `done` on the main thread.
    private func call(_ request: URLRequest, done: @escaping @MainActor ([String: Any]?) -> Void) {
        URLSession.shared.dataTask(with: request) { data, _, _ in
            let reply = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async { done(reply) }
        }.resume()
    }

    private func show() {
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Lights")
        image?.isTemplate = true
        statusItem.button?.image = image
        statusItem.button?.toolTip = summary
    }

    @objc private func openPage() {
        NSWorkspace.shared.open(hub)
    }
}

MainActor.assumeIsolated {  // top-level code runs on the main thread
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)  // menu bar only, no Dock icon
    withExtendedLifetime(LightsMenu()) { app.run() }
}
