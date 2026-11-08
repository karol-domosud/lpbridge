// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Karol Domosud

import AppKit
import CryptoKit

struct AppFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct Payload: Decodable {
    struct Asset: Decodable {
        let name: String
        let sha256: String
        let size: Int64
        let compressed: Bool
    }
    let version: String
    let assets: [Asset]
}

enum Installation {
    static let support = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/lpbridge")
    static let label = "com.lpbridge.app.agent"
    static let domain = "gui/\(getuid())"

    static func serviceNeedsUpdate(runningBuild: String?, resources: URL? = Bundle.main.resourceURL) -> Bool {
        guard let resources,
              let expected = try? String(contentsOf: resources.appendingPathComponent("service-build.txt"), encoding: .utf8)
                .trimmingCharacters(in: .whitespacesAndNewlines), !expected.isEmpty else { return false }
        return runningBuild != expected
    }

    static func output(_ executable: String, _ args: [String]) throws -> String {
        let process = Process(); let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: executable); process.arguments = args
        process.standardOutput = pipe; process.standardError = FileHandle.nullDevice
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // Only the listener on our port, owned by this login and running the Python
    // agent, may be stopped. Never kill by name or touch Podman's processes.
    static func stopLegacy() throws {
        let listeners = try output("/usr/sbin/lsof", ["-t", "-a", "-iTCP:8631", "-sTCP:LISTEN"])
        let pids = Set(listeners.split(separator: "\n").compactMap { Int32($0) })
        guard pids.count == 1, let pid = pids.first else {
            throw AppFailure(message: "The earlier service could not be identified safely. Close it using its own app, then try again.")
        }
        let owner = try output("/bin/ps", ["-p", String(pid), "-o", "uid="])
        let command = try output("/bin/ps", ["-p", String(pid), "-o", "command="])
        guard owner == String(getuid()), command.contains("Python") || command.contains("python"),
              command.hasSuffix(" -m agent") || command.contains(" -m agent ") else {
            throw AppFailure(message: "Another application is using the print service port. Close that application before continuing.")
        }
        // Stop KeepAlive before signalling; otherwise it can relaunch the old service.
        try run("/bin/launchctl", ["bootout", "\(domain)/\(label)"], allowFailure: true)
        // The old developer installer used this exact LaunchAgent label.
        try run("/bin/launchctl", ["bootout", "\(domain)/com.lpbridge.agent"], allowFailure: true)
        if kill(pid, 0) == 0 { _ = kill(pid, SIGINT) }
    }

    @discardableResult static func run(_ executable: String, _ args: [String], allowFailure: Bool = false) throws -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = args
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        if process.terminationStatus != 0 && !allowFailure {
            throw AppFailure(message: "macOS couldn’t start the background print service. Please try again. If it persists, open Diagnostics to share the startup log.")
        }
        return process.terminationStatus
    }

    // Runs off the UI thread. Progress measures bytes actually unpacked. Each
    // payload gets its own directory, so installing never overwrites an old VM.
    static func prepare(resources suppliedResources: URL? = nil,
                        supportRoot: URL = support, registerService: Bool = true,
                        progress: @escaping (String, Double?) -> Void) throws {
        let fm = FileManager.default
        let support = supportRoot
        try fm.createDirectory(at: support, withIntermediateDirectories: true)
        let lock = Darwin.open(support.appendingPathComponent(".setup.lock").path, O_RDWR | O_CREAT, 0o600)
        guard lock >= 0 else { throw AppFailure(message: "lpbridge couldn’t write its settings. Check available disk space and try again.") }
        defer { Darwin.close(lock) }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else {
            throw AppFailure(message: "Setup is already running in another lpbridge window. Let it finish, then try again.")
        }
        guard let resources = suppliedResources ?? Bundle.main.resourceURL else {
            throw AppFailure(message: "This app is incomplete. Please download a fresh copy.")
        }
        let payloadDir = resources.appendingPathComponent("payload")
        let manifestData = try Data(contentsOf: payloadDir.appendingPathComponent("manifest.json"))
        let payload = try JSONDecoder().decode(Payload.self, from: manifestData)
        let vm = support.appendingPathComponent("vm/\(payload.version)")
        let marker = vm.appendingPathComponent("installed.json")
        let total = payload.assets.reduce(Int64(0)) { $0 + $1.size }
        let installed = (try? Data(contentsOf: marker)) == manifestData && payload.assets.allSatisfy {
            let attributes = try? fm.attributesOfItem(atPath: vm.appendingPathComponent($0.name).path)
            return (attributes?[.size] as? NSNumber)?.int64Value == $0.size
        }
        try fm.createDirectory(at: support, withIntermediateDirectories: true)
        if !installed {
            try fm.createDirectory(at: vm, withIntermediateDirectories: true)
            for leftover in try fm.contentsOfDirectory(at: vm, includingPropertiesForKeys: nil)
                where leftover.lastPathComponent.hasPrefix(".") && leftover.pathExtension == "partial" {
                try fm.removeItem(at: leftover)
            }
            let available = (try fm.attributesOfFileSystem(forPath: support.path)[.systemFreeSize] as? NSNumber)?.int64Value ?? 0
            guard available > total + 512 * 1024 * 1024 else {
                throw AppFailure(message: "lpbridge needs about 4 GB of free space to prepare printing. Free up some space, then try again.")
            }
            try fm.createDirectory(at: vm, withIntermediateDirectories: true)
            var finished: Int64 = 0
            for asset in payload.assets {
                let source = payloadDir.appendingPathComponent(asset.name + (asset.compressed ? ".gz" : ""))
                let destination = vm.appendingPathComponent(asset.name)
                let temporary = vm.appendingPathComponent(".\(UUID().uuidString).partial")
                fm.createFile(atPath: temporary.path, contents: nil)
                defer { try? fm.removeItem(at: temporary) }
                let output = try FileHandle(forWritingTo: temporary)
                defer { try? output.close() }
                let process = Process()
                let input: FileHandle
                if asset.compressed {
                    let pipe = Pipe()
                    process.executableURL = URL(fileURLWithPath: "/usr/bin/gzip")
                    process.arguments = ["-dc", source.path]
                    process.standardOutput = pipe
                    process.standardError = FileHandle.nullDevice
                    try process.run()
                    input = pipe.fileHandleForReading
                } else { input = try FileHandle(forReadingFrom: source) }
                defer {
                    try? input.close()
                    if process.isRunning { process.terminate(); process.waitUntilExit() }
                }
                var hash = SHA256()
                var count: Int64 = 0
                var lastReport = Date.distantPast
                while let chunk = try input.read(upToCount: 1024 * 1024), !chunk.isEmpty {
                    try output.write(contentsOf: chunk)
                    hash.update(data: chunk)
                    count += Int64(chunk.count)
                    if Date().timeIntervalSince(lastReport) > 0.08 {
                        progress("Preparing your print engine", Double(finished + count) / Double(total))
                        lastReport = Date()
                    }
                }
                if asset.compressed {
                    process.waitUntilExit()
                    guard process.terminationStatus == 0 else {
                        throw AppFailure(message: "The print engine couldn’t be unpacked. Please download a fresh copy of lpbridge.")
                    }
                }
                guard count == asset.size, hash.finalize().map({ String(format: "%02x", $0) }).joined() == asset.sha256 else {
                    throw AppFailure(message: "The print engine didn’t pass its integrity check. Please download a fresh copy of lpbridge.")
                }
                try output.synchronize()
                if fm.fileExists(atPath: destination.path) { try fm.removeItem(at: destination) }
                try fm.moveItem(at: temporary, to: destination)
                finished += count
            }
            try manifestData.write(to: marker, options: .atomic)
        }
        if !registerService { return }
        progress("Making printing available at login", nil)
        let agents = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents")
        let logs = support.appendingPathComponent("logs")
        try fm.createDirectory(at: agents, withIntermediateDirectories: true)
        try fm.createDirectory(at: logs, withIntermediateDirectories: true)
        let plist = agents.appendingPathComponent("\(label).plist")
        let configuration: [String: Any] = [
            "Label": label,
            "ProgramArguments": [resources.appendingPathComponent("python/bin/python3").path,
                                 "-m", "agent", "--managed", "--runtime", "vfkit"],
            "WorkingDirectory": resources.path,
            "EnvironmentVariables": ["PYTHONPATH": resources.path, "PYTHONUNBUFFERED": "1",
                                     "PYTHONDONTWRITEBYTECODE": "1", "LPBRIDGE_VM_DIR": vm.path,
                                     "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"],
            "RunAtLoad": true, "KeepAlive": true, "ThrottleInterval": 15,
            "StandardOutPath": logs.appendingPathComponent("agent.log").path,
            "StandardErrorPath": logs.appendingPathComponent("agent.log").path
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: configuration, format: .xml, options: 0)
        try data.write(to: plist, options: .atomic)
        try run("/bin/launchctl", ["bootout", "\(domain)/\(label)"], allowFailure: true)
        try run("/bin/launchctl", ["bootstrap", domain, plist.path])
        progress("Waking up your print engine", nil)
    }
}
