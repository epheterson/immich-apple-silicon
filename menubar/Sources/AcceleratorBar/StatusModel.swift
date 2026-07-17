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
    var immichURL = ""

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
}

@MainActor
final class StatusModel: ObservableObject {
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
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
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
        s.dashboardUp = Self.pidAlive("dashboard") != nil
        s.version = Self.readVersion()
        let config = Self.readConfig()
        s.immichVersion = config["version"] as? String ?? ""
        s.immichURL = config["immich_url"] as? String ?? ""
        let mlPort = (config["ml_port"] as? Int) ?? 3003
        s.mlHealthy = await Self.ping(port: mlPort)
        snap = s
    }

    // MARK: - probes

    static func pidAlive(_ name: String) -> Int32? {
        let f = Paths.dataDir.appendingPathComponent("pids/\(name).pid")
        guard let text = try? String(contentsOf: f, encoding: .utf8),
              let pid = Int32(text.split(separator: "\n").first?
                  .trimmingCharacters(in: .whitespaces) ?? "")
        else { return nil }
        return kill(pid, 0) == 0 ? pid : nil
    }

    static func command(of pid: Int32) -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/ps")
        p.arguments = ["-p", "\(pid)", "-o", "command="]
        let out = Pipe()
        p.standardOutput = out
        try? p.run()
        // Read before waiting (pipe-buffer deadlock discipline; see Actions.run).
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        p.waitUntilExit()
        return text
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
