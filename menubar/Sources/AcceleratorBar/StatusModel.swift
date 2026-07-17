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

enum MLEngine: Equatable {
    case native, python, unknown

    var badge: String {
        switch self {
        case .native: return "NATIVE"
        case .python: return "PYTHON"
        case .unknown: return "—"
        }
    }
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

    // What "Open Immich" should launch: the public domain the user set in
    // Immich when present, otherwise the local URL the accelerator connects to.
    var openImmichURL: String {
        externalDomain.isEmpty ? immichURL : externalDomain
    }

    // The worker is actively chewing through jobs (drives the amber icon).
    var processing: Bool { jobsActive > 0 }

    var overall: ServiceState {
        if workerUp && mlHealthy { return .running }
        if !workerUp && !mlUp && !dashboardUp { return .stopped }
        return .degraded
    }
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
            Task { @MainActor in await self?.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        Task { await refresh() }
    }

    func stopPolling() { timer?.invalidate(); timer = nil }

    func refresh() async {
        var s = Snapshot()
        s.workerUp = Self.pidAlive("worker") != nil
        let mlPid = Self.pidAlive("ml")
        s.mlUp = mlPid != nil
        if let pid = mlPid {
            let cmd = Self.command(of: pid)
            if cmd.contains("immich-ml-native") { s.mlEngine = .native }
            else if cmd.contains("python") { s.mlEngine = .python }
        }
        if let dashPid = Self.pidAlive("dashboard") {
            // Confirm the tracked pid is really our dashboard, not a foreign
            // process a mis-adopted pidfile points at (older accelerators would
            // adopt whatever held the port, e.g. OrbStack).
            s.dashboardUp = Self.command(of: dashPid).contains("immich_accelerator")
        }
        s.version = Self.readVersion()
        let config = Self.readConfig()
        s.immichVersion = config["version"] as? String ?? ""
        s.immichURL = config["immich_url"] as? String ?? ""
        s.dashboardPort = (config["dashboard_port"] as? Int) ?? 8420
        s.externalDomain = await Self.externalDomain(base: s.immichURL)
        let mlPort = (config["ml_port"] as? Int) ?? 3003
        s.mlHealthy = await Self.ping(port: mlPort)
        if s.workerUp, let apiKey = config["api_key"] as? String {
            let jobs = await Self.jobCounts(base: s.immichURL, apiKey: apiKey)
            s.jobsActive = jobs.active
            s.jobsWaiting = jobs.waiting
        }
        snap = s
    }

    // Sum active/waiting across all of Immich's job queues so the menu bar can
    // show whether the worker is busy and how deep the backlog is. Immich
    // authenticates /api/jobs with the x-api-key header (key lives in config).
    static func jobCounts(base: String, apiKey: String) async -> (active: Int, waiting: Int) {
        guard !base.isEmpty, !apiKey.isEmpty, let url = URL(string: "\(base)/api/jobs")
        else { return (0, 0) }
        var req = URLRequest(url: url)
        req.timeoutInterval = 3
        req.setValue(apiKey, forHTTPHeaderField: "x-api-key")
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse, http.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return (0, 0) }
        var active = 0, waiting = 0
        for (_, value) in obj {
            guard let queue = value as? [String: Any],
                  let counts = queue["jobCounts"] as? [String: Any] else { continue }
            active += (counts["active"] as? Int) ?? 0
            waiting += (counts["waiting"] as? Int) ?? 0
        }
        return (active, waiting)
    }

    // Immich's public domain (set in admin settings), used so "Open Immich"
    // opens the address the user actually reaches Immich at rather than the
    // internal IP the worker connects to. Empty when unset or unreachable.
    static func externalDomain(base: String) async -> String {
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

    static func pidAlive(_ name: String) -> Int32? {
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

    static func command(of pid: Int32) -> String { psField(pid, "command=") }

    // One `ps` field for a pid, e.g. "command=" or "lstart=". Reads before
    // waiting (pipe-buffer deadlock discipline; see Actions.run).
    static func psField(_ pid: Int32, _ field: String) -> String {
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

    static func ping(port: Int) async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/ping") else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse else { return false }
        return http.statusCode == 200 && String(data: data, encoding: .utf8) == "pong"
    }

    static func readVersion() -> String {
        let f = Paths.optDir.appendingPathComponent("libexec/VERSION")
        return (try? String(contentsOf: f, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    static func readConfig() -> [String: Any] {
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
