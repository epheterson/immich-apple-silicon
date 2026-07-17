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
                // Drain the pipe BEFORE waiting: a child that fills the 64KB
                // pipe buffer (ml-test output does) would otherwise deadlock
                // against our waitUntilExit.
                let text = String(
                    data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                p.waitUntilExit()
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

    static func openDashboard(port: Int) {
        NSWorkspace.shared.open(URL(string: "http://localhost:\(port)")!)
    }

    static func openImmich(_ url: String) {
        if let u = URL(string: url.isEmpty ? "http://localhost:2283" : url) {
            NSWorkspace.shared.open(u)
        }
    }

    static func openLogs() {
        NSWorkspace.shared.open(Paths.dataDir.appendingPathComponent("logs"))
    }

    static func revealConfig() {
        NSWorkspace.shared.activateFileViewerSelecting([Paths.configFile])
    }

    // The one-liner shown in onboarding when the accelerator isn't installed.
    static let installCommand = "brew install epheterson/immich-accelerator/immich-accelerator"

    static func copyToPasteboard(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    // Setup is an interactive CLI flow (Docker/DB prompts), so hand it to
    // Terminal rather than trying to reproduce it in the panel.
    static func runSetupInTerminal() {
        let script = """
        tell application "Terminal"
            activate
            do script "\(cli) setup"
        end tell
        """
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        try? p.run()
    }

    // Switch the ML engine by rewriting config.json (preserving every other
    // key) and restarting so it takes effect. User-initiated from Settings.
    static func setMLEngine(_ engine: String) async {
        let url = Paths.configFile
        guard let data = try? Data(contentsOf: url),
              var obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return }
        obj["ml_engine"] = engine
        guard let out = try? JSONSerialization.data(
            withJSONObject: obj, options: [.prettyPrinted]) else { return }
        try? out.write(to: url, options: .atomic)
        await restartService()
    }
}
