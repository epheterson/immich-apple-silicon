import Foundation

// Reads accelerator truth directly: pidfiles, the ML service's /ping, and the
// installed VERSION/config. No daemons, no IPC; the same sources the CLI uses.
enum ServiceState: Equatable {
    case running, stopped, degraded

    var label: String {
        switch self {
        case .running: return "Running"
        case .stopped: return "Stopped"
        case .degraded: return "Degraded"
        }
    }
}

enum MLEngine: Equatable, Sendable {
    case native, python, unknown

    var badge: String {
        switch self {
        case .native: return "NATIVE"
        case .python: return "PYTHON"
        case .unknown: return "-"
        }
    }
}

/// One Immich processing queue's completion, as the dashboard reports it.
struct QueueProgress: Equatable, Identifiable {
    let key: String
    let label: String
    let done: Int
    let total: Int

    var id: String { key }
    var fraction: Double { total > 0 ? min(Double(done) / Double(total), 1) : 0 }
    var complete: Bool { total > 0 && done >= total }
    /// Clamped: done can briefly exceed total because the asset-total and
    /// per-stage-done counts are taken by separate queries and the library can
    /// change between them. "-3 remaining" is not a thing to show anyone.
    var remaining: Int { max(total - done, 0) }
}

struct Snapshot: Equatable {
    var workerUp = false
    var mlUp = false          // process alive
    var mlHealthy = false     // /ping answered
    var mlEngine: MLEngine = .unknown
    var dashboardUp = false
    var version = ""
    var immichVersion = ""
    var immichURL = ""       // the URL the worker connects to (from config)
    var externalDomain = ""  // Immich's configured public domain, if any
    var jobsActive = 0       // jobs the worker is running right now
    var jobsWaiting = 0      // jobs queued behind them
    var dashboardPort = 8420 // where the accelerator dashboard is served
    // Which components the user has turned on. A disabled component is hidden
    // entirely rather than shown red: "off because I said so" and "off because
    // it broke" are different facts and must not look the same.
    var workerEnabled = true
    var mlEnabled = true
    var dashboardEnabled = true
    var immichReachable = false // Immich answered on immich_url
    // Set while the ML service is fetching a model (the largest are several GB
    // and take minutes, during which Immich's jobs fail and retry).
    var downloadingModel = ""
    var downloadDone = 0
    var downloadTotal = 0
    // nil = not checked this cycle (the key is only exercised while the worker
    // is up). Distinguishing that from false matters: a stopped worker must not
    // make a perfectly good key look rejected.
    var apiKeyValid: Bool?
    /// Per-queue completion, populated only when the dashboard is serving.
    ///
    /// The dashboard is an enrichment here, never a dependency: 1.8.0 made the
    /// menu bar work with the dashboard switched off, and reading this from it
    /// must not quietly undo that. Empty means "we could not ask", and the
    /// panel falls back to the aggregate it computes from Immich directly.
    var queues: [QueueProgress] = []

    // What "Open Immich" should launch: the public domain the user set in
    // Immich when present, otherwise the local URL the accelerator connects to.
    var openImmichURL: String {
        externalDomain.isEmpty ? immichURL : externalDomain
    }

    // The worker is actively chewing through jobs (drives the amber icon).
    var processing: Bool { jobsActive > 0 }

    // Health is judged only against the components that actually process
    // photos, and only those the user turned on. Judging a disabled component
    // would leave an ML-only box amber forever for missing a worker it was told
    // not to run, which is the fastest way to teach someone to ignore the icon.
    //
    // The dashboard is deliberately excluded even when enabled: it is a way to
    // look at the work, not a way to do it. A wedged dashboard (or an OrbStack
    // process squatting port 8420) must not make a perfectly healthy install
    // report degraded. The Dashboard row still shows its own real state.
    var overall: ServiceState {
        var wanted = 0, healthy = 0
        if workerEnabled { wanted += 1; if workerUp { healthy += 1 } }
        if mlEnabled { wanted += 1; if mlHealthy { healthy += 1 } }

        // Nothing that processes photos is enabled. Note this is NOT the same
        // as "every component is off": a dashboard-only install lands here too,
        // and .stopped is still the honest answer, because from the point of
        // view of getting work done nothing is running. The Dashboard row
        // reports its own state separately.
        if wanted == 0 { return .stopped }

        if healthy == wanted { return .running }
        // Something is enabled and not healthy. Distinguish "not started yet"
        // from "started and broken", counting the dashboard as a sign of life
        // even though it does not count toward health.
        let anyAlive = workerUp || mlUp || dashboardUp
        return anyAlive ? .degraded : .stopped
    }
}

// The result of the blocking pidfile/ps/config probes, gathered off the main
// actor. Sendable (only value types) so it can cross back to the MainActor.
struct ProcessProbe: Sendable {
    var workerUp = false
    var mlUp = false
    var mlEngine: MLEngine = .unknown
    var dashboardUp = false
    var version = ""
    var immichVersion = ""
    var immichURL = ""
    var dashboardPort = 8420
    var workerEnabled = true
    var mlEnabled = true
    var dashboardEnabled = true
    var mlPort = 3003
    var apiKey = ""
}

// Filesystem locations shared by the model and actions (not actor-isolated).
enum Paths {
    static let dataDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".immich-accelerator")
    static let optDir = URL(fileURLWithPath: "/opt/homebrew/opt/immich-accelerator")
    static let configFile = dataDir.appendingPathComponent("config.json")

    // Is the accelerator CLI installed (via Homebrew)?
    static var isInstalled: Bool {
        FileManager.default.fileExists(atPath: optDir.appendingPathComponent("bin/immich-accelerator").path)
    }

    // Has the accelerator been set up (config.json written by `setup`)?
    static var isConfigured: Bool {
        FileManager.default.fileExists(atPath: configFile.path)
    }
}

@MainActor
final class StatusModel: ObservableObject {
    // One shared instance so the menu bar, the app delegate (first-run), and
    // the onboarding/settings windows all read the same live state.
    static let shared = StatusModel()

    @Published var snap = Snapshot()
    @Published var lastMLTest: String?
    @Published var busy = false

    private var timer: Timer?
    /// How many refreshes are awaiting their probes. The timer skips a tick
    /// while any is in flight rather than stacking a pass on top of a slow one:
    /// the probes have timeouts up to 6s and the panel polls every 3s, so
    /// without this a wedged endpoint would let ticks pile up.
    ///
    /// A count, not a flag. Six call sites refresh directly (the panel,
    /// Settings, onboarding, and startPolling's immediate kick), none of which
    /// consult this, so a single bool would be cleared by whichever overlapping
    /// pass returned first and the timer would start stacking again.
    private var inFlight = 0
    /// Ordering for those overlapping passes: each refresh takes a ticket, and
    /// a pass whose ticket is older than what has already been published drops
    /// its result. Without this a slow refresh could land a snapshot captured
    /// seconds earlier on top of a fresh one, which is exactly what happens
    /// right after a toggle, when an action-driven refresh races the poller.
    private var refreshTicket = 0
    private var publishedTicket = 0
    /// Set while the panel is open. Gates the one probe expensive enough that
    /// polling it in the background is a real cost to the user's Immich
    /// server, not just to us.
    var wantsQueueDetail = false

    init() {
        // Background cadence from launch so the menu-bar icon is accurate before
        // the panel is first opened; the panel bumps this to 3s while visible.
        startPolling(interval: 15)
    }

    func startPolling(interval: TimeInterval = 3) {
        timer?.invalidate()
        // Register in .common modes, not the default .scheduledTimer (.default
        // only): while the MenuBarExtra panel is open the run loop is in
        // event-tracking mode, and a .default-mode timer stops firing, so the
        // panel would freeze on whatever it showed when it opened.
        let t = Timer(timeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.inFlight == 0 else { return }
                await self.refresh()
            }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        Task { await refresh() }
    }

    func stopPolling() { timer?.invalidate(); timer = nil }

    func refresh() async {
        inFlight += 1
        refreshTicket += 1
        let ticket = refreshTicket
        defer { inFlight -= 1 }
        // The pidfile/ps/config probes fork subprocesses and touch the disk;
        // run them off the main actor so a poll never hitches the UI thread.
        let p = await Task.detached(priority: .utility) { Self.probeProcesses() }.value

        var s = Snapshot()
        s.workerUp = p.workerUp
        s.mlUp = p.mlUp
        s.mlEngine = p.mlEngine
        s.dashboardUp = p.dashboardUp
        s.version = p.version
        s.immichVersion = p.immichVersion
        s.immichURL = p.immichURL
        s.dashboardPort = p.dashboardPort
        s.workerEnabled = p.workerEnabled
        s.mlEnabled = p.mlEnabled
        s.dashboardEnabled = p.dashboardEnabled

        // The three network probes are independent (each has its own timeout),
        // so run them concurrently: a slow/unreachable service costs the max
        // latency, not the sum.
        async let domain = Self.externalDomain(base: p.immichURL)
        async let healthy = p.mlEnabled ? Self.ping(port: p.mlPort) : false
        async let jobs = Self.jobCounts(base: p.immichURL, apiKey: p.apiKey)
        async let reachable = Self.serverReachable(base: p.immichURL)
        // Only while the panel is open. /api/status is the most expensive
        // thing this app can ask for: a full aggregate over Immich's asset
        // table through a spawned psql (measured ~1.4s cold on a 174k-asset
        // library) plus a jobs API call, behind a cache shorter than our
        // background poll, so every background poll would miss. Merely having
        // the menu bar running would put a seconds-long query on the user's
        // Immich database every 15 seconds forever, to compute rows nobody is
        // looking at.
        //
        // Closed: nil, meaning "did not ask", so the last values survive and
        // are on screen the instant the panel opens. No dashboard: [], because
        // "nowhere to ask" really is no queue data, and keeping rows from
        // before it was switched off would show numbers nothing refreshes.
        async let queues: [QueueProgress]? = !p.dashboardUp ? []
            : (wantsQueueDetail ? Self.queueProgress(port: p.dashboardPort) : nil)
        async let fetching = p.mlUp ? Self.downloadProgress(port: p.mlPort) : nil
        s.externalDomain = await domain
        s.mlHealthy = await healthy
        if let f = await fetching {
            s.downloadingModel = f.model
            s.downloadDone = f.done
            s.downloadTotal = f.total
        }
        let counts = await jobs
        s.jobsActive = counts.active
        s.jobsWaiting = counts.waiting
        s.immichReachable = await reachable
        s.apiKeyValid = counts.authed
        // nil means the dashboard did not answer in time. Keep what we last
        // knew: a slow poll is not news, and dropping the rows for one cycle
        // resizes the panel under the user's pointer.
        s.queues = (await queues) ?? snap.queues

        // A pass that started later has already published: its snapshot is
        // newer than ours by construction, so leave it alone.
        guard ticket > publishedTicket else { return }
        publishedTicket = ticket
        snap = s
    }

    // Whether a component is switched on. Mirrors __main__._component_enabled:
    // absent means enabled, and an explicit key beats the legacy "ml_only"
    // preset so a box can be switched back without hand-editing config.json.
    nonisolated static func componentEnabled(_ name: String, _ config: [String: Any]) -> Bool {
        if let explicit = config[name] as? Bool { return explicit }
        if (config["ml_only"] as? Bool) == true { return name != "worker" }
        return true
    }

    /// The switches each preset sets, and their default when nothing is set.
    ///
    /// The CLI is authoritative: it applies presets and `encoding_preset` there
    /// decides the name. This mirror exists only so the settings window can
    /// show the current position without shelling out on every render. Keep the
    /// two tables in step; `ENCODING_PRESETS` in `__main__.py` is the original.
    nonisolated static let encodingPresets: [
        (name: String, switches: [String: Bool], mlEngine: String, stockML: Bool)
    ] = [
        ("stock", ["IMMICH_ACCEL_HW_VIDEO": false,
                   "IMMICH_ACCEL_HW_DECODE": false,
                   "IMMICH_ACCEL_HW_AUDIO": false], "python", true),
        // Audio is not part of this end; see ENCODING_PRESETS in __main__.py.
        ("apple-silicon", ["IMMICH_ACCEL_HW_VIDEO": true,
                           "IMMICH_ACCEL_HW_DECODE": true,
                           "IMMICH_ACCEL_HW_AUDIO": false], "native", false),
    ]

    /// Switches that are off unless asked for, because they change output.
    nonisolated static let encodingDefaultOff: Set<String> = ["IMMICH_ACCEL_HW_AUDIO"]

    /// Which preset the current switches spell, or "custom".
    nonisolated static func encodingPreset(_ config: [String: Any]) -> String {
        let engine = (config["ml_engine"] as? String) ?? "native"
        let stock = (config["stock_ml"] as? Bool) ?? false
        for preset in encodingPresets {
            guard preset.switches.allSatisfy({ encodingSwitchOn($0.key, config) == $0.value })
            else { continue }
            // The machine learning side too, exactly as encoding_preset does in
            // __main__.py. Checking only the encoding switches lets the pane
            // show Stock, asserting output identical to Docker, on an install
            // still running Vision and mlx.
            guard engine == preset.mlEngine, stock == preset.stockML else { continue }
            return preset.name
        }
        return "custom"
    }

    /// Whether an encoding switch reads as on, from the config `env` block.
    ///
    /// Mirrors the CLI's bool_setting: unset means on, and only these three
    /// spellings mean off. A real environment variable also wins at read time,
    /// but that is the background service's environment rather than this app's,
    /// so it cannot be checked from here; the CLI says so when it applies one.
    nonisolated static func encodingSwitchOn(_ name: String, _ config: [String: Any]) -> Bool {
        let defaultOn = !encodingDefaultOff.contains(name)
        guard let env = config["env"] as? [String: Any],
              let raw = env[name] else { return defaultOn }
        let value = String(describing: raw).trimmingCharacters(in: .whitespaces).lowercased()
        // Truthiness follows the default, matching bool_setting and the
        // wrapper: an unrecognised value keeps the safer position either way.
        return defaultOn
            ? !["0", "false", "no"].contains(value)
            : ["1", "true", "yes"].contains(value)
    }

    // Blocking pidfile/ps/config probes, gathered off the main actor. Confirms
    // the ML engine and that the tracked dashboard pid is really ours (not a
    // foreign process a mis-adopted pidfile points at, e.g. OrbStack).
    nonisolated static func probeProcesses() -> ProcessProbe {
        var p = ProcessProbe()
        p.workerUp = pidAlive("worker") != nil
        if let mlPid = pidAlive("ml") {
            p.mlUp = true
            let cmd = command(of: mlPid)
            if cmd.contains("immich-ml-native") { p.mlEngine = .native }
            else if cmd.contains("python") { p.mlEngine = .python }
        }
        if let dashPid = pidAlive("dashboard") {
            p.dashboardUp = command(of: dashPid).contains("immich_accelerator")
        }
        p.version = readVersion()
        let config = readConfig()
        p.immichVersion = config["version"] as? String ?? ""
        p.immichURL = config["immich_url"] as? String ?? ""
        p.dashboardPort = (config["dashboard_port"] as? Int) ?? 8420
        p.workerEnabled = componentEnabled("worker", config)
        p.mlEnabled = componentEnabled("ml", config)
        p.dashboardEnabled = componentEnabled("dashboard", config)
        p.mlPort = (config["ml_port"] as? Int) ?? 3003
        p.apiKey = (p.workerUp ? config["api_key"] as? String : nil) ?? ""
        return p
    }

    /// Per-queue completion from the accelerator's own dashboard API.
    ///
    /// We already compute this server-side for the web dashboard and have never
    /// read it here, so the panel showed one aggregate number while the data
    /// for five real progress bars sat one HTTP call away.
    ///
    /// Returns nil when we could not ask, which is not the same as "no queues"
    /// and must not be rendered as one. This endpoint runs real SQL against
    /// Immich's database behind a 5s cache: on a 174k-asset library it answers
    /// in ~20ms warm and ~1.4s cold, so a 3s poll pays the cold cost about
    /// every other time and a load spike pushes it past any tight timeout.
    /// Treating that as "no data" blanked the rows, and the panel visibly
    /// shrank and regrew every few seconds. The caller keeps the last good
    /// answer instead (see refresh).
    nonisolated static func queueProgress(port: Int) async -> [QueueProgress]? {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/status")
        else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 6
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let progress = root["progress"] as? [String: Any]
        else { return nil }

        // Fixed order and our own labels: the API keys are storage names, and
        // the order they serialize in is not a thing to show a person.
        let order = [
            ("thumbnails", "Thumbnails"), ("clip", "Search"),
            ("faces", "Faces"), ("ocr", "Text"), ("video", "Video"),
        ]
        return order.compactMap { key, label in
            guard let e = progress[key] as? [String: Any],
                  let total = (e["total"] as? NSNumber)?.intValue, total > 0,
                  let done = (e["done"] as? NSNumber)?.intValue
            else { return nil }
            return QueueProgress(key: key, label: label, done: done, total: total)
        }
    }

    // Sum active/waiting across all of Immich's job queues so the menu bar can
    // show whether the worker is busy and how deep the backlog is. Immich
    // authenticates /api/jobs with the x-api-key header (key lives in config).
    // Never call this without a key: an unauthenticated /api/jobs is a
    // guaranteed 401, and polling one every few seconds would fill the user's
    // Immich log for no benefit. `authed` is nil when we did not ask.
    nonisolated static func jobCounts(base: String, apiKey: String)
        async -> (active: Int, waiting: Int, authed: Bool?) {
        guard !base.isEmpty, !apiKey.isEmpty, let url = URL(string: "\(base)/api/jobs")
        else { return (0, 0, nil) }
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        req.setValue(apiKey, forHTTPHeaderField: "x-api-key")
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse else { return (0, 0, nil) }
        // 200 means the key was accepted; 401/403 means it was rejected. Any
        // other status says nothing about the key, so leave it unknown.
        guard http.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return (0, 0, [401, 403].contains(http.statusCode) ? false : nil) }
        var active = 0, waiting = 0
        for (_, value) in obj {
            guard let queue = value as? [String: Any],
                  let counts = queue["jobCounts"] as? [String: Any] else { continue }
            active += (counts["active"] as? Int) ?? 0
            waiting += (counts["waiting"] as? Int) ?? 0
        }
        return (active, waiting, true)
    }

    // Is Immich itself up? Unauthenticated and independent of the worker, so
    // "Immich reachable" stays correct while the accelerator is stopped.
    nonisolated static func serverReachable(base: String) async -> Bool {
        guard !base.isEmpty, let url = URL(string: "\(base)/api/server/ping")
        else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        guard let (_, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse else { return false }
        return http.statusCode == 200
    }

    // Immich's public domain (set in admin settings), used so "Open Immich"
    // opens the address the user actually reaches Immich at rather than the
    // internal IP the worker connects to. Empty when unset or unreachable.
    nonisolated static func externalDomain(base: String) async -> String {
        guard !base.isEmpty, let url = URL(string: "\(base)/api/server/config") else { return "" }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse, http.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let domain = obj["externalDomain"] as? String
        else { return "" }
        return domain
    }

    // MARK: - probes

    nonisolated static func pidAlive(_ name: String) -> Int32? {
        let f = Paths.dataDir.appendingPathComponent("pids/\(name).pid")
        guard let text = try? String(contentsOf: f, encoding: .utf8) else { return nil }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        guard let pid = Int32(lines.first?.trimmingCharacters(in: .whitespaces) ?? ""),
              kill(pid, 0) == 0
        else { return nil }
        // Guard against PID reuse: the accelerator writes the process start time
        // (`ps -o lstart`) on the pidfile's second line. If the PID has been
        // recycled to a different process (e.g. OrbStack squatting the dashboard
        // port), the start times won't match and this isn't really our process.
        let storedStart = lines.count > 1 ? lines[1].trimmingCharacters(in: .whitespaces) : ""
        if !storedStart.isEmpty {
            let actualStart = psField(pid, "lstart=")
            if !actualStart.isEmpty && actualStart != storedStart { return nil }
        }
        return pid
    }

    nonisolated static func command(of pid: Int32) -> String { psField(pid, "command=") }

    // One `ps` field for a pid, e.g. "command=" or "lstart=". Reads before
    // waiting (pipe-buffer deadlock discipline; see Actions.run).
    nonisolated static func psField(_ pid: Int32, _ field: String) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/ps")
        p.arguments = ["-p", "\(pid)", "-o", field]
        let out = Pipe()
        p.standardOutput = out
        try? p.run()
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        p.waitUntilExit()
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // What a long model fetch is doing, so the ML row can explain a stall that
    // would otherwise look like a broken service. nil when nothing is fetching.
    nonisolated static func downloadProgress(port: Int) async -> (model: String, done: Int, total: Int)? {
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse, http.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let d = obj["downloading"] as? [String: Any],
              let model = d["model"] as? String,
              let done = d["files_done"] as? Int,
              let total = d["files_total"] as? Int
        else { return nil }
        return (model, done, total)
    }

    nonisolated static func ping(port: Int) async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/ping") else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse else { return false }
        return http.statusCode == 200 && String(data: data, encoding: .utf8) == "pong"
    }

    nonisolated static func readVersion() -> String {
        let f = Paths.optDir.appendingPathComponent("libexec/VERSION")
        return (try? String(contentsOf: f, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    nonisolated static func readConfig() -> [String: Any] {
        let f = Paths.dataDir.appendingPathComponent("config.json")
        guard let data = try? Data(contentsOf: f),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return obj
    }

    // One-shot textual status for headless verification (see App.swift "status").
    @MainActor
    static func textStatus() async -> String {
        let model = StatusModel()
        await model.refresh()
        let s = model.snap
        return """
        version=\(s.version)
        worker=\(s.workerUp)
        ml_up=\(s.mlUp) ml_healthy=\(s.mlHealthy) engine=\(s.mlEngine.badge)
        dashboard=\(s.dashboardUp)
        immich=\(s.immichVersion) url=\(s.immichURL)
        overall=\(s.overall.label)
        """
    }
}
