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
    /// Both derived from Paths.brewPrefix, which is what decides which
    /// Homebrew this install belongs to. These used to resolve themselves,
    /// with two different rules between them; see brewPrefix for what that
    /// cost on a machine with both prefixes.
    static var brew: String { "\(Paths.brewPrefix)/bin/brew" }
    static var cli: String {
        "\(Paths.brewPrefix)/opt/immich-accelerator/bin/immich-accelerator"
    }
    static let service = "epheterson/immich-accelerator/immich-accelerator"
    /// What `brew services list` prints in its Name column. brew accepts
    /// the tap path as an argument but never echoes it back.
    static let listedService = "immich-accelerator"
    /// The tap, as `brew trust` wants it named.
    static let tap = "epheterson/immich-accelerator"

    /// The environment for brew calls that only read local state.
    ///
    /// No auto-update, because `brew outdated` git-fetches every tap once a
    /// day before answering and this runs when a window opens. No analytics,
    /// because that send is a detached curl which inherits our stdout and
    /// keeps the pipe open after brew itself has exited, so `run` waits on a
    /// process nobody is waiting for. No env hints, because they are noise in
    /// a parsed output.
    ///
    /// One constant rather than a literal at each call site: it was set on two
    /// of eight brew invocations when written out by hand.
    ///
    /// NOT for the two calls that ask whether a new version exists. A tap is a
    /// local git clone, and nothing but that fetch refreshes it, so suppressing
    /// it there made `brew outdated` permanently answer "nothing to do":
    /// Sparkle would update this app and the CLI core would never follow.
    /// Those use `brewEnvAllowingFetch`.
    static let brewEnv = [
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "HOMEBREW_NO_ANALYTICS": "1",
        "HOMEBREW_NO_ENV_HINTS": "1",
    ]

    /// For `outdated` and `upgrade`, which have to see the tap's new commits.
    /// Analytics and hints are still off: those only add output and a detached
    /// curl that holds our pipe open.
    static let brewEnvAllowingFetch = [
        "HOMEBREW_NO_ANALYTICS": "1",
        "HOMEBREW_NO_ENV_HINTS": "1",
    ]

    @discardableResult
    /// Run a tool and return its exit status and combined output.
    ///
    /// No timeout, deliberately. One was added in this release and removed
    /// again after three rounds of review found it broken three different
    /// ways: first it could not fire at all, because the deadline came after a
    /// read that only returns at EOF; then it fired on pipe EOF rather than
    /// process exit, so a command that succeeded in a second was reported as a
    /// timeout and killed; then the fix for that waited twice on a semaphore
    /// signalled once, adding a full second to every call in the app, and
    /// closed a file handle another thread was reading, which raises an
    /// Objective-C exception Swift cannot catch.
    ///
    /// It was there for a brew blocked on the Homebrew lock. The waiting is
    /// now the caller's problem rather than this function's: the Trust button
    /// says "Trusting...", says "Still working..." after ten seconds, and
    /// settles on the real answer whenever it arrives (see startTrust in
    /// SettingsView). Nothing is cancelled, so brew is never signalled halfway
    /// through writing trust.json, and a command that succeeds slowly is
    /// reported as the success it was.
    static func run(_ tool: String, _ args: [String],
                    env: [String: String] = [:]) async -> (Int32, String) {
        await withCheckedContinuation { cont in
            DispatchQueue.global().async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: tool)
                p.arguments = args
                if !env.isEmpty {
                    // Overlay, not replacement: the child still needs PATH and
                    // the rest of the inherited environment to find its tools.
                    p.environment = ProcessInfo.processInfo.environment
                        .merging(env) { _, new in new }
                }
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
                    data: out.fileHandleForReading.readDataToEndOfFile(),
                    encoding: .utf8) ?? ""
                p.waitUntilExit()
                cont.resume(returning: (p.terminationStatus, text))
            }
        }
    }

    /// What happened when a setting tried to take effect.
    ///
    /// Three outcomes, not two. Collapsing them is what let the pane report a
    /// change as applied while the running accelerator carried on with the old
    /// setting, and separately what let a settings change start an accelerator
    /// somebody had deliberately stopped.
    enum ApplyResult {
        /// Restarted, so the running accelerator uses the new setting now.
        case applied
        /// Nothing running, so there is nothing to apply yet.
        case stopped
        /// Alive, but not started by `brew services`, which is the only lever
        /// available here: brew will not restart a process it did not start.
        case runningOutsideBrew

        /// Empty when there is nothing to tell the user.
        var message: String {
            switch self {
            case .applied:
                return ""
            case .stopped:
                return "Saved. Takes effect when you start the accelerator."
            case .runningOutsideBrew:
                return "Saved. The accelerator is running outside brew services, "
                    + "so restart it there to pick this up."
            }
        }
    }

    /// Apply a configuration change to the running accelerator where that is
    /// possible, and report which of the three cases happened.
    ///
    /// Deliberately not `brew services restart` on its own: that starts a
    /// stopped service, so changing a setting would start the accelerator and
    /// set it processing. Applying a setting must never be the thing that
    /// starts work.
    /// Whether Homebrew is refusing to load our formula because the tap is
    /// untrusted.
    ///
    /// This is not the same as "no update available", and telling them apart
    /// is the whole point: `brew outdated` prints nothing in both cases, so
    /// coreOutdated() returns nil either way and the app offered nothing while
    /// the Mac sat on an old version indefinitely. Measured on the release Mac
    /// by removing the tap from trust.json: brew prints "Refusing to load
    /// formula ... from untrusted tap" and `brew info --json` returns no
    /// formula at all.
    ///
    /// The words are the same two the CLI matches, pinned by a test.
    static func brewRefusesTap() async -> Bool {
        guard isBrewInstall else { return false }
        // HOMEBREW_NO_AUTO_UPDATE, because `brew outdated` git-fetches every
        // tap once a day before answering, measured at 68 seconds. This runs
        // whenever the Settings window opens, so without it opening Settings
        // can stall on a network fetch nobody asked for. App.swift already
        // guards coreOutdated() for the same cost, but that path is
        // conditional and this one is a routine user action.
        let (_, out) = await run(
            brew, ["outdated", "--formula", listedService],
            env: brewEnv)
        // On the line that names us. brew emits refusals for other untrusted
        // taps too, and matching anywhere in the output meant someone else's
        // tap put "Updates are blocked" on our pane, with a button that
        // succeeds and changes nothing.
        return out.lowercased().split(separator: "\n").contains { line in
            line.contains(listedService)
                && (line.contains("untrusted tap")
                    || line.contains("refusing to load formula"))
        }
    }

    /// Trust the tap, which is what the button offers to do. Only ever from an
    /// explicit click: it changes the user's Homebrew configuration.
    static func trustTap() async -> (ok: Bool, output: String) {
        let (code, out) = await run(
            brew, ["trust", tap],
            env: brewEnv)
        return (code == 0, out)
    }

    /// Whether `brew services list` says our service is started.
    ///
    /// Split out and reachable from the command line (`AcceleratorBar
    /// brew-parse`, the same affordance as `parse-mounts`) because the
    /// decision not to restart rests entirely on reading brew's table
    /// correctly, and the table's shape is a property of the installed brew
    /// rather than of anything in this repository. Reading the code proves
    /// nothing about it; feeding it the real output does.
    ///
    /// Matched against `listedService`, not `service`: `brew services` takes
    /// the fully qualified tap path as an argument but prints the short
    /// formula name, so comparing against the tap path matches nothing and
    /// reports every install as stopped.
    ///
    /// Name and status are compared as whole fields, and the column runs are
    /// several spaces wide. Measured on the release Mac:
    ///
    ///     immich-accelerator started         elp  ~/Library/LaunchAgents/...
    ///     immich-accelerator none
    ///
    /// A stopped service reads `none`, and its row ends after the status, so
    /// nothing may assume a User or File column is present. `run` also merges
    /// stderr into this text, and brew emits unrelated deprecation warnings
    /// from other taps on every invocation, so the match has to be anchored on
    /// the name rather than on finding a word anywhere in the output.
    static func brewHasItStarted(_ list: String) -> Bool {
        list.split(separator: "\n").contains { line in
            let fields = line.split(separator: " ", omittingEmptySubsequences: true)
            return fields.count > 1
                && fields[0] == listedService
                && fields[1] == "started"
        }
    }

    @discardableResult
    static func applyToRunningService() async -> ApplyResult {
        let (_, list) = await run(brew, ["services", "list"], env: brewEnv)
        if brewHasItStarted(list) {
            await restartService()
            return .applied
        }
        // Not brew's to restart. A live pid still means it is running, and
        // saying "takes effect when you start it" would be wrong.
        let alive = await withCheckedContinuation { cont in
            DispatchQueue.global().async {
                cont.resume(returning: StatusModel.pidAlive("worker") != nil
                    || StatusModel.pidAlive("ml") != nil)
            }
        }
        return alive ? .runningOutsideBrew : .stopped
    }

    /// Move every encoding switch to a named position, through the CLI, which
    /// is what defines the position.
    static func setEncodingPreset(_ name: String) async -> (ok: Bool, message: String) {
        await runEncoding(["encoding", "preset", name])
    }

    /// Flip one encoding switch, through the same CLI.
    static func setEncodingSwitch(_ name: String, _ on: Bool) async -> (ok: Bool, message: String) {
        await runEncoding(["encoding", name, on ? "on" : "off"])
    }

    private static func runEncoding(_ args: [String]) async -> (ok: Bool, message: String) {
        guard isBrewInstall else { return (false, "Could not reach the accelerator CLI.") }
        let (code, out) = await run(cli, args)
        guard code == 0 else {
            let detail = out.split(separator: "\n")
                .last(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty })
                .map(String.init) ?? "exit \(code)"
            return (false, detail)
        }
        // Success, with a message only when the change is saved but not live.
        return (true, await applyToRunningService().message)
    }

    /// Transcode one file every way this Mac can and return the report.
    static func encodeCompare(_ path: String) async -> String {
        guard isBrewInstall else { return "Could not reach the accelerator CLI." }
        let (_, out) = await run(cli, ["compare", path, "--no-open"])
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

    static func startService() async { await run(brew, ["services", "start", service], env: brewEnv) }
    static func stopService() async { await run(brew, ["services", "stop", service], env: brewEnv) }
    static func restartService() async { await run(brew, ["services", "restart", service], env: brewEnv) }

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
    /// Write the chosen engine and apply it, reporting what happened.
    ///
    /// Returning Void made a failed read, a failed encode, a failed write and
    /// a change made while stopped all look identical to success.
    @discardableResult
    static func setMLEngine(_ engine: String) async -> (ok: Bool, message: String) {
        let url = Paths.configFile
        guard let data = try? Data(contentsOf: url),
              var obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return (false, "Could not read the configuration.") }
        obj["ml_engine"] = engine
        guard let out = try? JSONSerialization.data(
            withJSONObject: obj, options: [.prettyPrinted])
        else { return (false, "Could not encode the configuration.") }
        do {
            try out.write(to: url, options: .atomic)
        } catch {
            return (false, "Could not save: \(error.localizedDescription)")
        }
        return (true, await applyToRunningService().message)
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
    //
    // The exit code is deliberately not consulted. `brew outdated` with a named
    // formula is an assertion, not a report: Homebrew sets a failure status when
    // the formula it was asked about IS outdated (cmd/outdated.rb, `Homebrew.
    // failed = args.named.present? && outdated.present?`). Measured on this
    // machine: `brew outdated --formula --verbose git` prints
    // "git (2.46.0) < 2.55.0" and exits 1. Guarding on `code == 0` therefore
    // threw the answer away in precisely the case that matters, and an app
    // updated by Sparkle went on running the old CLI core forever.
    static func coreOutdated() async -> String? {
        let (_, out) = await run(
            brew, ["outdated", "--formula", "--verbose", "immich-accelerator"],
            env: brewEnvAllowingFetch)
        // Anchored to the line naming the formula. run() merges stderr into
        // this string, so a bare search for "< " can lift a version out of a
        // message about something else entirely.
        guard
            let line = out.split(separator: "\n").first(where: {
                $0.contains("immich-accelerator") && $0.contains("< ")
            }),
            let r = line.range(of: "< ")
        else { return nil }
        let v = line[r.upperBound...]
            .prefix { !$0.isWhitespace }
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return v.isEmpty ? nil : v
    }

    // Upgrade the core formula. The watch loop applies the new code on its own
    // (stops the stale worker, relaunches), so no explicit restart is needed on
    // the standard launchd install.
    @discardableResult
    static func upgradeCore() async -> Bool {
        let (code, _) = await run(brew, ["upgrade", "immich-accelerator"], env: brewEnvAllowingFetch)
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
