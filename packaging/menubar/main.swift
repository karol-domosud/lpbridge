// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Karol Domosud

// lpbridge menubar app.
//
// A thin AppKit client for the agent's loopback control API -- it holds no
// state of its own and does no printing. Everything it shows comes from
// http://127.0.0.1:<port>/api/..., and every action is a call back to it.
//
// Build:  swiftc -O -swift-version 5 -o lpbridge-menubar main.swift -framework AppKit
// (no Xcode project; Command Line Tools are enough)

import AppKit
import Foundation

let agentPort = ProcessInfo.processInfo.environment["LPBRIDGE_PORT"] ?? "8631"
let base = "http://127.0.0.1:\(agentPort)"

// MARK: - API model

struct Job: Decodable {
    let id: Int
    let name: String?
    let state: Int
    let error: String?
}

struct Printer: Decodable {
    let id: String
    let name: String
    let make_and_model: String?
    let transport: String?
    let offline: Bool
    let queued: Int
    let fault: [String]?
    let jobs: [Job]?
}

struct Device: Decodable {
    let id: String
    let name: String
    let uri: String?
    let driver: String?
    let available: Bool?
    let note: String?
    let serving: Bool
}

private struct PrintersEnvelope: Decodable { let printers: [Printer] }
private struct DevicesEnvelope: Decodable { let devices: [Device] }

enum API {
    static func get<T: Decodable>(_ path: String, as: T.Type,
                                  done: @escaping (T?) -> Void) {
        guard let url = URL(string: base + path) else { return done(nil) }
        var req = URLRequest(url: url)
        req.timeoutInterval = 6
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data = data,
                  let value = try? JSONDecoder().decode(T.self, from: data)
            else { return done(nil) }
            done(value)
        }.resume()
    }

    static func mutate(_ path: String, method: String, body: [String: String]? = nil,
                       done: @escaping (Bool) -> Void) {
        guard let url = URL(string: base + path) else { return done(false) }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = 30
        req.setValue("1", forHTTPHeaderField: "X-lpbridge")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body = body { req.httpBody = try? JSONSerialization.data(withJSONObject: body) }
        URLSession.shared.dataTask(with: req) { _, response, _ in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            done((200..<300).contains(code))
        }.resume()
    }
}

// MARK: - Controller

final class Controller: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var printers: [Printer] = []
    var devices: [Device] = []
    var agentUp = false
    var timer: Timer?

    override init() {
        super.init()
        menu.delegate = self
        item.menu = menu
        setIcon(active: false)
        rebuild()
        refresh()
        refreshDevices(force: false)
        // /api/discover reads a cache the agent refreshes on its own thread,
        // so polling it is cheap and the menu is populated before first open
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refresh()
            self?.refreshDevices(force: false)
        }
    }

    private func setIcon(active: Bool) {
        let name = active ? "printer.fill" : "printer"
        let image = NSImage(systemSymbolName: name,
                            accessibilityDescription: "lpbridge")
        image?.isTemplate = true
        item.button?.image = image
    }

    // MARK: refresh

    func refresh() {
        API.get("/api/printers", as: PrintersEnvelope.self) { [weak self] env in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.agentUp = env != nil
                self.printers = env?.printers ?? []
                self.setIcon(active: self.agentUp && !self.printers.isEmpty)
                self.rebuild()
            }
        }
    }

    func refreshDevices(force: Bool) {
        API.get("/api/discover" + (force ? "?force=1" : ""),
                as: DevicesEnvelope.self) { [weak self] env in
            DispatchQueue.main.async {
                self?.devices = env?.devices ?? []
                self?.rebuild()
            }
        }
    }

    func menuWillOpen(_ menu: NSMenu) {
        refresh()
        refreshDevices(force: false)
    }

    // MARK: menu

    private func statusLine(_ p: Printer) -> String {
        if let fault = p.fault, fault.count > 1 { return fault[1] }
        if p.offline { return "offline" }
        if p.queued > 0 { return "\(p.queued) queued" }
        return "ready"
    }

    private func bullet(_ p: Printer) -> String {
        if p.fault != nil { return "●" }        // red-ish: rendered as text
        if p.offline { return "○" }
        if p.queued > 0 { return "◐" }
        return "●"
    }

    func rebuild() {
        menu.removeAllItems()

        if !agentUp {
            menu.addItem(disabled("lpbridge is not running"))
            menu.addItem(action("Start lpbridge", #selector(startAgent)))
            menu.addItem(.separator())
            menu.addItem(action("Quit", #selector(quit)))
            return
        }

        if printers.isEmpty {
            menu.addItem(disabled("No printers yet"))
        } else {
            for p in printers {
                let title = "\(bullet(p))  \(p.name) — \(statusLine(p))"
                let entry = NSMenuItem(title: title, action: nil, keyEquivalent: "")
                entry.submenu = printerSubmenu(p)
                menu.addItem(entry)
            }
        }

        let addable = devices.filter { !$0.serving && ($0.available ?? false) }
        if !addable.isEmpty {
            menu.addItem(.separator())
            menu.addItem(disabled("Available"))
            for d in addable {
                let label = "\(d.name) — \(d.driver ?? "driver found")"
                let entry = action("Add \(label)", #selector(addDevice(_:)))
                entry.representedObject = d.uri ?? d.name
                menu.addItem(entry)
            }
        }

        menu.addItem(.separator())
        menu.addItem(action("Open lpbridge…", #selector(openApp)))
        menu.addItem(action("Rescan for Printers", #selector(rescan)))
        menu.addItem(.separator())
        menu.addItem(action("Quit", #selector(quit)))
    }

    private func printerSubmenu(_ p: Printer) -> NSMenu {
        let sub = NSMenu()
        sub.addItem(disabled(p.make_and_model ?? "unknown driver"))
        if let t = p.transport { sub.addItem(disabled(t)) }
        let recent = (p.jobs ?? []).prefix(5)
        if !recent.isEmpty {
            sub.addItem(.separator())
            sub.addItem(disabled("Recent jobs"))
            for j in recent {
                let state = ["3": "pending", "5": "printing", "9": "done",
                             "8": "failed", "7": "canceled"][String(j.state)]
                    ?? "state \(j.state)"
                sub.addItem(disabled("#\(j.id) \(j.name ?? "Untitled") — \(state)"))
            }
        }
        sub.addItem(.separator())
        let remove = action("Remove This Printer", #selector(removePrinter(_:)))
        remove.representedObject = p.id
        sub.addItem(remove)
        return sub
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.isEnabled = false
        return i
    }

    private func action(_ title: String, _ sel: Selector) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: sel, keyEquivalent: "")
        i.target = self
        return i
    }

    // MARK: actions

    @objc func openApp() {
        // the setup app is the front end; there is no web page on the agent's port
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.lpbridge.app") {
            NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
        } else {
            warn("The lpbridge app is not installed. Open the DMG and drag it to Applications.")
        }
    }

    @objc func rescan() { refreshDevices(force: true) }

    @objc func addDevice(_ sender: NSMenuItem) {
        guard let token = sender.representedObject as? String else { return }
        API.mutate("/api/printers", method: "POST", body: ["device": token]) { ok in
            DispatchQueue.main.async {
                if !ok { self.warn("Could not add that printer.") }
                self.refresh(); self.refreshDevices(force: true)
            }
        }
    }

    @objc func removePrinter(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String else { return }
        let alert = NSAlert()
        alert.messageText = "Remove this printer?"
        alert.informativeText = "Its macOS print queue will be removed too."
        alert.addButton(withTitle: "Remove")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        API.mutate("/api/printers/\(id)", method: "DELETE") { _ in
            DispatchQueue.main.async { self.refresh(); self.refreshDevices(force: true) }
        }
    }

    @objc func startAgent() {
        // the agent lives next to us inside lpbridge.app
        let here = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/lpbridge")
        let proc = Process()
        if FileManager.default.isExecutableFile(atPath: here.path) {
            proc.executableURL = here
        } else {
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            proc.arguments = ["-m", "agent"]
            proc.currentDirectoryURL = Bundle.main.bundleURL
                .appendingPathComponent("Contents/Resources")
        }
        do { try proc.run() } catch { warn("Could not start lpbridge: \(error)") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { self.refresh() }
    }

    @objc func quit() { NSApp.terminate(nil) }

    private func warn(_ text: String) {
        let a = NSAlert(); a.messageText = "lpbridge"; a.informativeText = text
        a.runModal()
    }
}

// MARK: - main

let app = NSApplication.shared
app.setActivationPolicy(.accessory)      // menubar only, no Dock icon
let controller = Controller()
app.run()
