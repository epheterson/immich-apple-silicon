import AppKit
import Foundation

// Daily actions, shelling out to the same commands a user would run.
enum Actions {
    static let brew = "/opt/homebrew/bin/brew"
    static let cli = "/opt/homebrew/opt/immich-accelerator/bin/immich-accelerator"
    static let service = "epheterson/immich-accelerator/immich-accelerator"

    @discardableResult
    static func run(_ tool: String, _ args: [String]) async -> (Int32, String) {
        await withCheckedContinuation { cont in
            DispatchQueue.global().async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: tool)
                p.arguments = args
                let out = Pipe()
                p.standardOutput = out
                p.standardError = out
                do { try p.run() } catch {
                    cont.resume(returning: (-1, "\(error)")); return
                }
                p.waitUntilExit()
                let text = String(
                    data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                cont.resume(returning: (p.terminationStatus, text))
            }
        }
    }

    static func startService() async { await run(brew, ["services", "start", service]) }
    static func stopService() async { await run(brew, ["services", "stop", service]) }
    static func restartService() async { await run(brew, ["services", "restart", service]) }

    // Runs the CLI's real ML test and condenses the result to one line.
    static func mlTest() async -> String {
        let (code, out) = await run(cli, ["ml-test"])
        if let line = out.split(separator: "\n").last(where: { $0.contains("checks passed") }) {
            // e.g. "ML service OK — 4/4 checks passed"
            return line.replacingOccurrences(of: #"^\S+\s+\S+\s+"#, with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)
        }
        return code == 0 ? "passed" : "failed (exit \(code))"
    }

    static func openDashboard() {
        NSWorkspace.shared.open(URL(string: "http://localhost:8420")!)
    }

    static func openImmich(_ url: String) {
        if let u = URL(string: url.isEmpty ? "http://localhost:2283" : url) {
            NSWorkspace.shared.open(u)
        }
    }

    static func openLogs() {
        NSWorkspace.shared.open(Paths.dataDir.appendingPathComponent("logs"))
    }
}
