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

// Remembers which SMB shares were mounted when the user turned this on, and
// remounts any that go missing at every launch. macOS drops NAS shares
// silently under network/sleep churn (see split-deployment troubleshooting);
// remounting is a manual "Connect to Server" nobody remembers to redo after
// a wake, so photos quietly stop processing — worker.log fills with ENOENT
// on every file the worker touches, but nothing in the UI says why.
enum MountSharesAtLogin {
    private static let key = "MountSharesAtLoginURLs"

    static var isEnabled: Bool { UserDefaults.standard.data(forKey: key) != nil }

    // Turning it on snapshots whichever SMB shares are mounted right now.
    // Turning it off forgets that list — re-enabling later re-snapshots
    // rather than remounting something the user may have unmounted on purpose.
    static func set(_ on: Bool) async {
        if on {
            let (_, out) = await Actions.run("/sbin/mount", [])
            let found = shares(fromMountOutput: out)
            let encoded = (try? JSONEncoder().encode(found)) ?? Data()
            UserDefaults.standard.set(encoded, forKey: key)
        } else {
            UserDefaults.standard.removeObject(forKey: key)
        }
    }

    /// One remembered share: where it came from, and where it was actually
    /// mounted. Both, because they are not derivable from each other.
    struct Share: Codable {
        var url: String
        var mountPoint: String
    }

    /// Parses `mount` output into shares worth remembering.
    ///
    /// A real line looks like:
    ///   //user@host/share on /Volumes/immich (smbfs, nodev, nosuid)
    /// but mount points contain spaces, are not always under /Volumes, and
    /// some smbfs mounts are automounts the user never asked for. Checked
    /// against real output from the release Mac rather than an invented
    /// sample, which is where the last two of those came from.
    static func shares(fromMountOutput out: String) -> [Share] {
        out.split(separator: "\n").compactMap { line -> Share? in
            // Split on " on " once, then take the mount point up to the last
            // " (", since the mount point itself may contain spaces.
            guard let onRange = line.range(of: " on "),
                  let optsRange = line.range(of: " (", options: .backwards),
                  optsRange.lowerBound > onRange.upperBound
            else { return nil }

            let remote = String(line[line.startIndex..<onRange.lowerBound])
            let mountPoint = String(line[onRange.upperBound..<optsRange.lowerBound])
            let opts = String(line[optsRange.upperBound...])

            guard opts.contains("smbfs"), remote.hasPrefix("//") else { return nil }
            // Automounts: Time Machine's own SMB mount appears exactly like a
            // user share but was never mounted by hand, and remounting it at
            // every login is both wrong and visible.
            guard !opts.contains("nobrowse") else { return nil }

            return Share(url: "smb:" + remote, mountPoint: mountPoint)
        }
    }

    // Called once at launch. Best-effort and silent: `open` hands off to
    // Finder/diskarbitrationd, which only prompts for credentials if none
    // were saved to Keychain from the share's original manual mount.
    static func remountMissing() {
        guard let data = UserDefaults.standard.data(forKey: key),
              let remembered = try? JSONDecoder().decode([Share].self, from: data)
        else { return }
        for share in remembered {
            // The mount point we saw, not one derived from the share name.
            // macOS appends -1 on a collision and the user can mount a share
            // anywhere, so re-deriving it checks a path that may never have
            // existed and remounts something already mounted.
            guard !FileManager.default.fileExists(atPath: share.mountPoint),
                  let url = URL(string: share.url) else { continue }
            NSWorkspace.shared.open(url)
        }
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

    static func startService() async { await run(brew, ["services", "start", service]) }
    static func stopService() async { await run(brew, ["services", "stop", service]) }
    static func restartService() async { await run(brew, ["services", "restart", service]) }

    /// Restart only a service that is already running.
    ///
    /// `brew services restart` starts a stopped service, so using it to apply a
    /// setting takes someone who deliberately stopped the accelerator, perhaps
    /// while their NAS is down, and starts it transcoding because they flipped
    /// a switch to have it ready for later. Applying a setting must never be
    /// the thing that starts processing.
    ///
    /// Returns whether it restarted, so the caller can say "takes effect when
    /// you start it" rather than claiming it already has.
    @discardableResult
    static func restartIfRunning() async -> Bool {
        let (_, out) = await run(brew, ["services", "list"])
        let running = out.split(separator: "\n").contains { line in
            line.hasPrefix("immich-accelerator") && line.contains("started")
        }
        if running { await restartService() }
        return running
    }

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
    /// Transcode one file both ways and return the report as text. Slow by
    /// nature (it runs five real encodes), so the caller shows a busy state.
    static func encodeCompare(_ path: String) async -> String {
        guard isBrewInstall else { return "Could not reach the accelerator CLI." }
        let (_, out) = await run(cli, ["encode-compare", path])
        // The CLI logs with a timestamp prefix, which is noise in a window
        // that is not a log.
        let cleaned = out.split(separator: "\n", omittingEmptySubsequences: false)
            .map { line -> String in
                let s = String(line)
                guard let r = s.range(of: #"^\d\d:\d\d:\d\d (INFO|WARNING|ERROR)\s+"#,
                                      options: .regularExpression) else { return s }
                return String(s[r.upperBound...])
            }
            .joined(separator: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned.isEmpty ? "No output." : cleaned
    }

    /// Move every encoding switch to a named position. The CLI owns what each
    /// position means, so the app never has to spell out a preset itself.
    static func setEncodingPreset(_ name: String) async -> (ok: Bool, message: String) {
        guard isBrewInstall else { return (false, "Could not reach the accelerator CLI.") }
        let (code, out) = await run(cli, ["encoding", "preset", name])
        if code == 0 {
            // Same rule as the individual switches: apply now if it is
            // running, never start it because a setting changed.
            await restartIfRunning()
            return (true, "")
        }
        let detail = out.split(separator: "\n")
            .last(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty })
            .map(String.init) ?? "exit \(code)"
        return (false, detail)
    }

    /// Flip one encoding switch through the CLI, which owns what a switch
    /// means and which variable it writes. Deliberately not a direct
    /// config.json write like setMLEngine: the value has to agree with what
    /// ffmpeg-wrapper.sh reads, and there is a test pinning the CLI to the
    /// wrapper. A second implementation here would not be covered by it.
    static func setEncodingSwitch(_ name: String, _ on: Bool) async -> (ok: Bool, message: String) {
        guard isBrewInstall else { return (false, "Could not reach the accelerator CLI.") }
        let (code, out) = await run(cli, ["encoding", name, on ? "on" : "off"])
        if code == 0 {
            // Applies now if the accelerator is running, and does not start
            // it if it is not.
            await restartIfRunning()
            return (true, "")
        }
        let detail = out.split(separator: "\n")
            .last(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty })
            .map(String.init) ?? "exit \(code)"
        return (false, detail)
    }

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
