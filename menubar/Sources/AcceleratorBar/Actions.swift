import AppKit
import Foundation
import ServiceManagement

// Single source of truth for the login-item state, so the menu panel and the
// settings window don't each hand-roll (and diverge on) the register logic.
enum LaunchAtLogin {
    static var isEnabled: Bool { SMAppService.mainApp.status == .enabled }
    static func set(_ on: Bool) {
        try? on ? SMAppService.mainApp.register() : SMAppService.mainApp.unregister()
    }
}

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

    /// Run a command and deliver its output line by line as it arrives.
    ///
    /// `run` above buffers to completion, which is fine for a status probe and
    /// useless for setup: extracting the server and building the venv take
    /// minutes, and a wizard that shows nothing for minutes is indistinguishable
    /// from one that has hung. Returns the exit status once the process ends.
    @discardableResult
    static func stream(
        _ tool: String, _ args: [String],
        stdin: String? = nil,
        onLine: @escaping @Sendable (String) -> Void
    ) async -> Int32 {
        await withCheckedContinuation { cont in
            DispatchQueue.global().async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: tool)
                p.arguments = args
                // No TTY here, so setup's prompts would read EOF and answer no.
                // Callers pass --yes; this just makes sure nothing waits on a
                // human who cannot answer.
                //
                // `stdin` carries secrets to `--secrets-stdin`. They go on a
                // pipe rather than in `args` because argv is readable by every
                // process on the machine through ps.
                let inPipe = stdin.map { _ in Pipe() }
                p.standardInput = inPipe ?? FileHandle.nullDevice
                let out = Pipe()
                p.standardOutput = out
                p.standardError = out
                do { try p.run() } catch {
                    onLine("failed to launch: \(error)")
                    cont.resume(returning: -1)
                    return
                }
                if let inPipe, let text = stdin {
                    inPipe.fileHandleForWriting.write(Data(text.utf8))
                    try? inPipe.fileHandleForWriting.close()
                }
                var pending = ""
                let handle = out.fileHandleForReading
                while true {
                    let chunk = handle.availableData
                    if chunk.isEmpty { break }
                    pending += String(data: chunk, encoding: .utf8) ?? ""
                    while let nl = pending.firstIndex(of: "\n") {
                        let line = String(pending[pending.startIndex..<nl])
                        pending = String(pending[pending.index(after: nl)...])
                        if !line.trimmingCharacters(in: .whitespaces).isEmpty {
                            onLine(line)
                        }
                    }
                }
                if !pending.trimmingCharacters(in: .whitespaces).isEmpty { onLine(pending) }
                p.waitUntilExit()
                cont.resume(returning: p.terminationStatus)
            }
        }
    }

    /// Install the formula, the way the README tells people to.
    ///
    /// `brew trust` matters and is easy to miss: Homebrew 5.1.15+ silently
    /// skips untrusted third-party taps during `brew upgrade`, so without it
    /// the install works and then never updates again.
    static func installFormula(onLine: @escaping @Sendable (String) -> Void) async -> Bool {
        onLine("Installing the accelerator with Homebrew. This takes a few minutes.")
        let code = await stream(brew, ["install", service], onLine: onLine)
        if code != 0 { return false }
        onLine("Trusting the tap so future upgrades reach you...")
        await stream(brew, ["trust", "epheterson/immich-accelerator"], onLine: onLine)
        return Paths.isInstalled
    }

    /// Ask the CLI what Immich this Mac can see.
    ///
    /// The wizard needs to know whether Immich runs here in Docker or
    /// elsewhere, and the app must not answer that itself: `cmd_setup`
    /// dispatches on it, so a second implementation here could disagree with
    /// the one that acts. `detect` changes nothing and prints only JSON.
    static func detect() async -> WizardModel.Detection {
        guard Paths.isInstalled else {
            return .init(note: "The accelerator command line isn't installed yet, so nothing can be detected until it is.")
        }
        let (code, out) = await run(cli, ["detect"])
        guard code == 0,
              let data = out.data(using: .utf8),
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else {
            // Almost always an installed core older than this app, which has no
            // `detect` subcommand. Say so instead of quietly guessing, because
            // the guess used to be "remote" for everybody.
            return .init(note: "This version of the accelerator can't report what's running on this Mac. Choose below.")
        }
        var d = WizardModel.Detection(askedSuccessfully: true)
        d.dockerFound = json["docker"] is String
        if let local = json["local"] as? [String: Any] {
            d.immichVersion = local["version"] as? String
            d.mediaLocation = local["media_location"] as? String
        }
        // The CLI's own wording, so the screen says the same thing the terminal
        // would rather than a second guess at the cause.
        d.note = (json["local_error"] as? String) ?? (json["docker_error"] as? String)
        return d
    }

    /// Run setup non-interactively and stream it. `--yes` is what makes this
    /// possible at all; without it every prompt reads EOF and answers no,
    /// including "start now?".
    static func runSetup(
        url: String, apiKey: String, mlOnly: Bool,
        remote: WizardModel.RemoteDetails? = nil,
        onLine: @escaping @Sendable (String) -> Void
    ) async -> Bool {
        var args = ["setup", "--yes"]
        var secrets: String?
        if mlOnly {
            args.append("--ml-only")
        } else if !url.isEmpty {
            // Only for a server on another machine. With no --url, cmd_setup
            // takes the local Docker path and reads the database and Redis
            // credentials out of the running container instead of asking.
            args += ["--url", url]
            if !apiKey.isEmpty { args += ["--api-key", apiKey] }
            if let r = remote {
                args += [
                    "--db-host", r.dbHost, "--db-port", r.dbPort,
                    "--db-user", r.dbUser, "--db-name", r.dbName,
                    "--redis-host", r.redisHost, "--redis-port", r.redisPort,
                ]
                if !r.redisUser.isEmpty { args += ["--redis-user", r.redisUser] }
                if !r.mediaPath.isEmpty { args += ["--upload-mount", r.mediaPath] }
                // Passwords never go in args; --secrets-stdin reads them here.
                args.append("--secrets-stdin")
                let payload: [String: String] = [
                    "db_password": r.dbPassword, "redis_password": r.redisPassword,
                ]
                secrets = String(
                    data: (try? JSONSerialization.data(withJSONObject: payload)) ?? Data(),
                    encoding: .utf8)
            }
        }
        let code = await stream(cli, args, stdin: secrets, onLine: onLine)
        return code == 0
    }

    // MARK: - config backup

    /// Copy config.json somewhere the user chooses. The API key lives in this
    /// file, so the picker (rather than a fixed path) is deliberate: the user
    /// decides where a secret lands.
    static func backupConfig(to url: URL) throws {
        try FileManager.default.copyItem(at: Paths.configFile, to: url)
    }

    /// Replace config.json from a backup, after checking it parses and looks
    /// like ours. Restoring garbage would leave the accelerator unable to
    /// start with no clue why.
    static func restoreConfig(from url: URL) throws {
        let data = try Data(contentsOf: url)
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { throw ConfigError.notJSON }
        // Any real config has at least one of these. A stricter check would
        // reject the legitimate ml-only shape, which has almost nothing in it.
        let known = ["ml_port", "immich_url", "server_dir", "ml_only", "worker", "ml"]
        guard known.contains(where: { obj[$0] != nil }) else { throw ConfigError.notOurs }
        try FileManager.default.createDirectory(
            at: Paths.dataDir, withIntermediateDirectories: true)

        // Staged, not deleted-then-copied. The old order removed the live
        // config first, so a copy that failed afterwards (the backup living on
        // a volume that went away between the read above and the write, say)
        // left the Mac with no config at all and no way back. Write the
        // replacement beside it, permission it, and only then swap.
        let staged = Paths.configFile.deletingLastPathComponent()
            .appendingPathComponent(".config-restore-\(UUID().uuidString).json")
        try data.write(to: staged, options: .atomic)
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: staged.path)
        do {
            if FileManager.default.fileExists(atPath: Paths.configFile.path) {
                _ = try FileManager.default.replaceItemAt(Paths.configFile, withItemAt: staged)
            } else {
                try FileManager.default.moveItem(at: staged, to: Paths.configFile)
            }
        } catch {
            try? FileManager.default.removeItem(at: staged)
            throw error
        }
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: Paths.configFile.path)
    }

    enum ConfigError: LocalizedError {
        case notJSON, notOurs
        var errorDescription: String? {
            switch self {
            case .notJSON: return "That file isn't JSON."
            case .notOurs: return "That JSON isn't an accelerator config."
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
            // e.g. "ML service OK - 4/4 checks passed"
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

    /// Is `path` present, a directory, and readable by us right now?
    ///
    /// Deliberately a real read rather than a `fileExists` check: an SMB share
    /// that has gone away can leave a mount point that still stats fine, and a
    /// path can exist while being unreadable. The distinction matters because
    /// the fixes differ, so the note names which one it is.
    static func probeLibrary(_ path: String) async -> (ok: Bool, note: String) {
        await withCheckedContinuation { cont in
            DispatchQueue.global().async {
                let fm = FileManager.default
                var isDir: ObjCBool = false
                guard fm.fileExists(atPath: path, isDirectory: &isDir) else {
                    cont.resume(returning: (false,
                        "Nothing at that path yet. If the library lives on a NAS, connect to the share first."))
                    return
                }
                guard isDir.boolValue else {
                    cont.resume(returning: (false, "That path is a file, not a folder."))
                    return
                }
                guard let entries = try? fm.contentsOfDirectory(atPath: path) else {
                    cont.resume(returning: (false,
                        "The folder is there but can't be read. Check permissions, or reconnect the share."))
                    return
                }
                // Immich's media root has these beside each other. Naming what
                // is missing beats "looks wrong": pointing at the parent of the
                // real library is the single most common mistake here.
                let expected = ["library", "upload", "thumbs", "encoded-video"]
                let present = expected.filter { entries.contains($0) }
                if present.isEmpty {
                    cont.resume(returning: (false,
                        "Readable, but this doesn't look like Immich's media folder: none of library/, upload/, thumbs/ are in it."))
                } else {
                    cont.resume(returning: (true,
                        "Readable, and contains \(present.joined(separator: ", "))."))
                }
            }
        }
    }

    /// Hand off to the system's own Connect to Server flow. macOS shows its
    /// authentication sheet and keeps credentials in the keychain, so the app
    /// neither sees nor stores a password.
    static func openFileServerConnect(hint: String) {
        let host = hint.split(separator: ":").first.map(String.init) ?? hint
        if !host.isEmpty, let url = URL(string: "smb://\(host)") {
            NSWorkspace.shared.open(url)
        } else {
            // No host to guess at: open the Finder command that asks for one.
            NSAppleScript(source: """
            tell application "Finder" to activate
            tell application "System Events" to keystroke "k" using command down
            """)?.executeAndReturnError(nil)
        }
    }

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

    // Enable/disable one component via the CLI, which flips its config key and
    // starts/stops it now. The other components are untouched, so no full
    // service restart is needed. Returns false when the CLI is missing or the
    // command failed, so the caller can undo the switch instead of showing a
    // state the accelerator never reached.
    // Returns the CLI's own output on failure rather than a generic message:
    // turning the worker back on runs a full `start`, which can fail for real
    // reasons (Docker down, media not mounted, sharp needs a rebuild), and the
    // CLI already explains each one better than the UI could.
    static func setComponent(_ name: String, _ on: Bool) async -> (ok: Bool, message: String) {
        guard isBrewInstall else { return (false, "Could not reach the accelerator CLI.") }
        let (code, out) = await run(cli, ["component", name, on ? "on" : "off"])
        if code == 0 { return (true, "") }
        let detail = out.split(separator: "\n")
            .last(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty })
            .map(String.init) ?? "exit \(code)"
        return (false, detail)
    }

    // Was the core installed by Homebrew? The CLI lives under the formula's opt
    // prefix, so its presence is the check. A from-source install has no
    // formula to upgrade.
    static var isBrewInstall: Bool {
        FileManager.default.isExecutableFile(atPath: cli)
    }

    // The available core-formula version if brew says one is ready, else nil.
    // `brew outdated --verbose` prints "immich-accelerator (1.7.1) < 1.7.2" and
    // stays silent for an up-to-date OR pinned formula, so nil correctly means
    // "do not run an upgrade".
    static func coreOutdated() async -> String? {
        let (code, out) = await run(
            brew, ["outdated", "--formula", "--verbose", "immich-accelerator"])
        guard code == 0, let r = out.range(of: "< ") else { return nil }
        let v = out[r.upperBound...]
            .prefix { !$0.isWhitespace }
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return v.isEmpty ? nil : v
    }

    // Upgrade the core formula. The watch loop applies the new code on its own
    // (stops the stale worker, relaunches), so no explicit restart is needed on
    // the standard launchd install.
    @discardableResult
    static func upgradeCore() async -> Bool {
        let (code, _) = await run(brew, ["upgrade", "immich-accelerator"])
        return code == 0
    }

    // True if semver `a` (\"X.Y.Z\") is newer than `b`. Non-numeric suffixes are
    // dropped; unparseable inputs return false (never triggers a downgrade).
    static func versionNewer(_ a: String, than b: String) -> Bool {
        let pa = a.split(separator: ".").compactMap { Int($0) }
        let pb = b.split(separator: ".").compactMap { Int($0) }
        guard !pa.isEmpty, !pb.isEmpty else { return false }
        for i in 0 ..< max(pa.count, pb.count) {
            let x = i < pa.count ? pa[i] : 0
            let y = i < pb.count ? pb[i] : 0
            if x != y { return x > y }
        }
        return false
    }
}
