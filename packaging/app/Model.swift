// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Karol Domosud

import AppKit
import SwiftUI

struct Printer: Decodable, Identifiable {
    let id: String
    let name: String
    let offline: Bool
    let queued: Int
    let fault: [String]?
    let jobs: [PrintJob]
}
struct PrintJob: Decodable, Identifiable {
    let id: Int
    let name: String?
    let state: Int
    let error: String?
    var status: String {
        [3: "Waiting", 4: "Held", 5: "Printing", 6: "Stopped", 7: "Cancelled",
         8: "Couldn’t print", 9: "Completed"][state] ?? "Waiting"
    }
}
struct DriverCandidate: Decodable, Identifiable, Hashable {
    let ppd_name: String
    let driver: String?
    let make_and_model: String?
    let source: String?
    var id: String { ppd_name }
    var label: String {
        let name = driver ?? ppd_name
        if let model = make_and_model, !model.isEmpty, model != name { return "\(name) — \(model)" }
        return name
    }
}
struct Device: Decodable, Identifiable {
    let id: String
    let name: String
    let uri: String?
    let transport: String?
    let driver: String?
    let available: Bool?
    let note: String?
    let serving: Bool
    var candidates: [DriverCandidate]? = nil
}
struct PrintersResponse: Decodable { let printers: [Printer] }
struct DiscoveryResponse: Decodable { let devices: [Device]; let scanning: Bool }
struct Health: Decodable { let app_protocol: Int?; let managed: Bool?; let service_build: String? }
struct Reply: Decodable { let message: String?; let error: String? }

enum Service {
    static let base = URL(string: "http://127.0.0.1:8631")!
    static func request<T: Decodable>(_ path: String, method: String = "GET",
                                      body: [String: Any]? = nil) async throws -> T {
        var request = URLRequest(url: URL(string: path, relativeTo: base)!)
        request.timeoutInterval = method == "GET" ? 150 : 900
        request.httpMethod = method
        if method != "GET" {
            request.setValue("1", forHTTPHeaderField: "X-lpbridge")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try body.map { try JSONSerialization.data(withJSONObject: $0) }
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let reply = try? JSONDecoder().decode(Reply.self, from: data)
            throw AppFailure(message: reply?.error ?? reply?.message ?? "The print service couldn’t finish this request. Please try again.")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }
    struct DriverReply: Decodable { let ok: Bool?; let kind: String?; let detail: String?; let error: String? }
    /// Upload a .ppd / .ppd.gz / .deb the user supplied; the engine installs it.
    static func uploadDriver(_ url: URL) async throws -> DriverReply {
        var request = URLRequest(url: URL(string: "/api/drivers", relativeTo: base)!)
        request.timeoutInterval = 1500
        request.httpMethod = "POST"
        request.setValue("1", forHTTPHeaderField: "X-lpbridge")
        request.setValue(url.lastPathComponent, forHTTPHeaderField: "X-lpbridge-filename")
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await URLSession.shared.upload(for: request, fromFile: url)
        let reply = try JSONDecoder().decode(DriverReply.self, from: data)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode), reply.ok == true else {
            throw AppFailure(message: reply.error ?? "The print service couldn’t install that file.")
        }
        return reply
    }
    static func health() async -> Health? {
        var request = URLRequest(url: base.appendingPathComponent("api/health"))
        request.timeoutInterval = 2
        guard let (data, _) = try? await URLSession.shared.data(for: request) else { return nil }
        return try? JSONDecoder().decode(Health.self, from: data)
    }
}

@MainActor final class AppModel: ObservableObject {
    let preview = CommandLine.arguments.contains("--preview")
    enum Page { case welcome, preparing, discovery, library, success }
    @Published var page: Page = .welcome
    @Published var stage = "Preparing your print engine"
    @Published var progress: Double?
    @Published var devices: [Device] = []
    @Published var printers: [Printer] = []
    @Published var scanning = false
    @Published var busy = false
    @Published var error: String?
    @Published var selected: String?
    @Published var query = ""
    @Published var addedName = ""
    @Published var connected = false
    @Published var legacyFound = false
    private var poller: Task<Void, Never>?
    private var discoveryInFlight = false
    init() {
        if preview {
            let scene = CommandLine.arguments.last ?? "welcome"
            page = ["welcome": .welcome, "preparing": .preparing, "discovery": .discovery,
                    "library": .library, "success": .success, "error": .preparing][scene] ?? .welcome
            connected = scene != "welcome" && scene != "preparing" && scene != "error"
            progress = scene == "preparing" ? 0.42 : nil
            if scene == "error" { error = "The print engine is taking longer than expected. Try again, or open Diagnostics to share the startup log." }
            devices = [Device(id: "preview-usb", name: "Samsung ML-2580N", uri: nil, transport: "usb-raw", driver: "splix", available: true, note: "exact match for this model", serving: false),
                       Device(id: "preview-network", name: "Office printer", uri: nil, transport: "jetdirect", driver: "IPP Everywhere (driverless)", available: true, note: nil, serving: false)]
            printers = [Printer(id: "preview", name: "Samsung ML-2580N", offline: false, queued: 0, fault: nil,
                                jobs: [PrintJob(id: 1, name: "Weekend plans.pdf", state: 9, error: nil)])]
            addedName = "Samsung ML-2580N"
        }
    }
    var visibleDevices: [Device] {
        devices.filter { !$0.serving && (query.isEmpty || $0.name.localizedCaseInsensitiveContains(query)) }
    }
    func connectIfRunning() async {
        guard !preview else { return }
        if let health = await Service.health(), health.app_protocol == 1, health.managed == true {
            if Installation.serviceNeedsUpdate(runningBuild: health.service_build) {
                await start(); return
            }
            connected = true
            await refreshPrinters()
            page = printers.isEmpty ? .discovery : .library
            startPolling()
        } else if UserDefaults.standard.bool(forKey: "setupComplete") { await start() }
    }
    func start() async {
        if preview { page = .discovery; return }
        guard !busy else { return }
        busy = true; error = nil; page = .preparing
        defer { busy = false }
        do {
            let userApplications = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications").path + "/"
            if !Bundle.main.bundleURL.path.hasPrefix("/Applications/") && !Bundle.main.bundleURL.path.hasPrefix(userApplications) {
                stage = "Moving lpbridge into Applications"; progress = nil
                let source = Bundle.main.bundleURL
                let destination = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications/lpbridge.app")
                try await Task.detached {
                    try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
                    guard !FileManager.default.fileExists(atPath: destination.path) else {
                        throw AppFailure(message: "lpbridge is already in your Applications folder. Open that copy, or replace it with this one in Finder.")
                    }
                    try FileManager.default.copyItem(at: source, to: destination)
                }.value
                try await NSWorkspace.shared.openApplication(at: destination, configuration: .init())
                NSApp.terminate(nil)
                return
            }
            var needsStart = true
            if let health = await Service.health() {
                guard health.app_protocol == 1, health.managed == true else {
                    legacyFound = true
                    throw AppFailure(message: "An earlier lpbridge service is running. Switch to this version to finish setup. Existing print queues will be kept; you can choose your printers again here.")
                }
                needsStart = Installation.serviceNeedsUpdate(runningBuild: health.service_build)
                if needsStart {
                    let current: PrintersResponse = try await Service.request("/api/printers")
                    guard current.printers.allSatisfy({ $0.queued == 0 }) else {
                        throw AppFailure(message: "Let current print jobs finish, then reopen lpbridge to update the print service.")
                    }
                    stage = "Updating your print service"
                    try await Task.detached { try Installation.stopLegacy() }.value
                    for _ in 0..<20 {
                        if await Service.health() == nil { break }
                        try await Task.sleep(nanoseconds: 500_000_000)
                    }
                    guard await Service.health() == nil else {
                        throw AppFailure(message: "The previous print service is still closing. Please try again.")
                    }
                }
            }
            if needsStart {
                try await Task.detached {
                    try Installation.prepare { text, value in
                        Task { @MainActor in self.stage = text; self.progress = value }
                    }
                }.value
                var ready = false
                for _ in 0..<60 {
                    if let health = await Service.health(), health.managed == true,
                       !Installation.serviceNeedsUpdate(runningBuild: health.service_build) { ready = true; break }
                    try await Task.sleep(nanoseconds: 1_000_000_000)
                }
                guard ready else { throw AppFailure(message: "The print engine is taking longer than expected. Try again, or open Diagnostics to share the startup log.") }
            }
            connected = true
            UserDefaults.standard.set(true, forKey: "setupComplete")
            await refreshPrinters()
            page = printers.isEmpty ? .discovery : .library
            startPolling()
        } catch { self.error = error.localizedDescription }
    }
    func startPolling() {
        poller?.cancel()
        poller = Task {
            while !Task.isCancelled {
                await refreshPrinters()
                if page == .discovery { await discover() }
                try? await Task.sleep(nanoseconds: 4_000_000_000)
            }
        }
    }
    func migrate() async {
        guard !busy else { return }
        busy = true; error = nil; stage = "Updating your print service"; progress = nil
        do {
            let current: PrintersResponse = try await Service.request("/api/printers")
            guard current.printers.allSatisfy({ $0.queued == 0 }) else {
                throw AppFailure(message: "There are print jobs in progress. Let them finish before switching to the new app.")
            }
            try await Task.detached { try Installation.stopLegacy() }.value
            for _ in 0..<20 {
                if await Service.health() == nil { break }
                try await Task.sleep(nanoseconds: 1_000_000_000)
            }
            guard await Service.health() == nil else { throw AppFailure(message: "The earlier service hasn’t finished closing yet. Try switching again in a moment.") }
            legacyFound = false; busy = false
            await start()
        } catch { self.error = error.localizedDescription; busy = false }
    }
    func refreshPrinters() async {
        guard !preview else { return }
        do {
            let response: PrintersResponse = try await Service.request("/api/printers")
            printers = response.printers; connected = true
        } catch { connected = false }
    }
    @Published var driverNotice: String?
    /// Driver files dropped or chosen by the user: .ppd, .ppd.gz or .deb.
    func addDriverFiles(_ urls: [URL]) async {
        let accepted = urls.filter { ["ppd", "deb", "gz"].contains($0.pathExtension.lowercased()) }
        guard !accepted.isEmpty else { driverNotice = "Only .ppd, .ppd.gz and .deb files can be installed."; return }
        busy = true; error = nil
        defer { busy = false }
        for url in accepted {
            driverNotice = "Installing \(url.lastPathComponent) into the print engine…"
            do {
                let reply = try await Service.uploadDriver(url)
                driverNotice = reply.kind == "ppd" ? "Installed \(url.lastPathComponent). Printers are being re-matched."
                                                   : "Installed \(url.lastPathComponent) and its Linux dependencies."
            } catch { driverNotice = "Could not install \(url.lastPathComponent): \(error.localizedDescription)" }
        }
        await discover(force: true)
    }
    func chooseDriverFile() {
        let panel = NSOpenPanel()
        panel.title = "Choose a driver file"
        panel.message = "A CUPS PPD (.ppd, .ppd.gz) or a Debian package (.deb) with the printer's Linux driver."
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        if panel.runModal() == .OK { Task { await addDriverFiles(panel.urls) } }
    }
    func discover(force: Bool = false) async {
        guard !preview else { return }
        guard !busy && !discoveryInFlight else { return }
        discoveryInFlight = true; scanning = true
        defer { discoveryInFlight = false }
        do {
            let response: DiscoveryResponse = try await Service.request("/api/discover" + (force ? "?force=1" : ""))
            devices = response.devices; scanning = response.scanning
        } catch { scanning = false; self.error = "We couldn’t search for printers. Check the print service and try again." }
    }
    func add(_ device: Device, ppd: String? = nil) async {
        if preview { addedName = device.name; page = .success; return }
        busy = true; error = nil
        page = .preparing
        stage = "Setting up \(device.name)"
        progress = nil
        defer { busy = false }
        do {
            var body: [String: Any] = ["device": device.id]
            if let ppd = ppd { body["ppd"] = ppd }
            let _: Reply = try await Service.request("/api/printers", method: "POST", body: body)
            addedName = device.name
            await refreshPrinters()
            page = .success
        } catch { self.error = error.localizedDescription }
    }
    func remove(_ printer: Printer) async {
        if preview { printers.removeAll { $0.id == printer.id }; return }
        busy = true
        defer { busy = false }
        do {
            let _: Reply = try await Service.request("/api/printers/\(printer.id)", method: "DELETE")
            await refreshPrinters()
        } catch { self.error = error.localizedDescription }
    }
    func diagnostics() { NSWorkspace.shared.open(Installation.support.appendingPathComponent("logs")) }
    /// Terminal.app running the bundled `lpbridge-shell`: it boots the engine
    /// through the service, keeps it up while the window is open, and drops
    /// the user into a root ssh session. For the installers a file drop
    /// cannot run (Samsung ULD, HP's plugin, ...).
    func openEngineTerminal() {
        // `open -a Terminal <executable>` runs it in a new window, with no
        // Apple Events permission prompt (an AppleScript "do script" would need one)
        let script = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/bin/lpbridge-shell").path
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        task.arguments = ["-a", "Terminal", script]
        do { try task.run() } catch { self.error = "Could not open Terminal. Run this yourself: \(script)" }
    }
    func showTransferFolder() {
        let folder = Installation.support.appendingPathComponent("vm-spool/transfer")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        NSWorkspace.shared.open(folder)
    }
}
