// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Karol Domosud
//
// lpbridge-authorize REASON
//
// Ask the person at the Mac to prove they are the account owner -- Touch ID,
// Apple Watch, or the account password -- before the agent does something
// only a person should decide (opening a root shell in the engine). Exit 0
// when they did, 1 when they declined or failed, 2 when no such
// authentication is available on this Mac, so the caller can fall back.
//
// Built by packaging/build_app.py into Contents/Resources/bin; the agent runs
// it and trusts nothing but the exit code.

import Foundation
import LocalAuthentication

let reason = CommandLine.arguments.dropFirst().joined(separator: " ")
let context = LAContext()
context.localizedCancelTitle = "Don’t Allow"
var unavailable: NSError?
guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &unavailable) else {
    FileHandle.standardError.write(("authentication unavailable: \(unavailable?.localizedDescription ?? "unknown")\n").data(using: .utf8)!)
    exit(2)
}
let done = DispatchSemaphore(value: 0)
var status: Int32 = 1
context.evaluatePolicy(.deviceOwnerAuthentication,
                       localizedReason: reason.isEmpty ? "open a root shell in the print engine" : reason) { ok, error in
    if ok {
        status = 0
    } else if let error = error {
        FileHandle.standardError.write(("\(error.localizedDescription)\n").data(using: .utf8)!)
    }
    done.signal()
}
_ = done.wait(timeout: .now() + 120)
exit(status)
