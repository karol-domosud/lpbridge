// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Karol Domosud

import Foundation
import CryptoKit

@main struct BootstrapTests {
    static func main() throws {
        let fm = FileManager.default
        if CommandLine.arguments.count == 3 {
            try Installation.prepare(resources: URL(fileURLWithPath: CommandLine.arguments[1]),
                                     supportRoot: URL(fileURLWithPath: CommandLine.arguments[2]), registerService: false) { text, value in
                if value == nil { print(text) }
            }
            print("Real bundled payload installed and verified.")
            return
        }
        let root = fm.temporaryDirectory.appendingPathComponent("lpbridge-bootstrap-\(UUID().uuidString)")
        defer { try? fm.removeItem(at: root) }
        let resources = root.appendingPathComponent("resources")
        try fm.createDirectory(at: resources, withIntermediateDirectories: true)
        try "new-service-build\n".write(to: resources.appendingPathComponent("service-build.txt"), atomically: true, encoding: .utf8)
        precondition(Installation.serviceNeedsUpdate(runningBuild: nil, resources: resources))
        precondition(Installation.serviceNeedsUpdate(runningBuild: "old-service-build", resources: resources))
        precondition(!Installation.serviceNeedsUpdate(runningBuild: "new-service-build", resources: resources))
        let payload = resources.appendingPathComponent("payload")
        let support = root.appendingPathComponent("support")
        try fm.createDirectory(at: payload, withIntermediateDirectories: true)
        let original = Data(repeating: 42, count: 1024 * 1024)
        let source = payload.appendingPathComponent("engine.img")
        try original.write(to: source)
        try Installation.run("/usr/bin/gzip", ["-k", source.path])
        let sha = SHA256.hash(data: original).map { String(format: "%02x", $0) }.joined()
        let manifest: [String: Any] = ["version": "fixture", "assets": [["name": "engine.img", "size": original.count, "compressed": true, "sha256": sha]]]
        let manifestURL = payload.appendingPathComponent("manifest.json")
        try JSONSerialization.data(withJSONObject: manifest).write(to: manifestURL)
        var progress: [Double] = []
        try Installation.prepare(resources: resources, supportRoot: support, registerService: false) { _, value in
            if let value { progress.append(value) }
        }
        precondition(!progress.isEmpty && progress.allSatisfy { (0...1).contains($0) })
        let unpacked = try Data(contentsOf: support.appendingPathComponent("vm/fixture/engine.img"))
        precondition(unpacked == original)
        // A completed install is reused, even if its compressed source is gone.
        try fm.removeItem(at: payload.appendingPathComponent("engine.img.gz"))
        try Installation.prepare(resources: resources, supportRoot: support, registerService: false) { _, _ in }
        try Data("corrupt".utf8).write(to: payload.appendingPathComponent("engine.img.gz"))
        let marker = support.appendingPathComponent("vm/fixture/installed.json")
        try fm.removeItem(at: marker)
        do {
            try Installation.prepare(resources: resources, supportRoot: support, registerService: false) { _, _ in }
            fatalError("Corrupted payload was accepted")
        } catch { print("Corrupted payload rejected: \(error.localizedDescription)") }
        precondition(!fm.fileExists(atPath: marker.path))
        let preserved = try Data(contentsOf: support.appendingPathComponent("vm/fixture/engine.img"))
        precondition(preserved == original)
        let contents = try fm.contentsOfDirectory(atPath: support.appendingPathComponent("vm/fixture").path)
        precondition(!contents.contains { $0.hasSuffix(".partial") })
        print("PASS: extraction, checksum, progress, reuse, corruption rejection, atomic cleanup")
    }
}
