import SwiftUI

// A single validated configuration/health fact, shown in Settings and included
// in the copy-for-issue text. Read-only; computed from config + the live
// snapshot + a couple of cheap filesystem checks (no blocking work here — the
// network-derived facts come pre-probed off the snapshot).
struct Check: Identifiable {
    enum Level { case ok, warn, fail, info }
    let id = UUID()
    let label: String
    let detail: String
    let level: Level

    var iconName: String {
        switch level {
        case .ok: return "checkmark.circle.fill"
        case .warn: return "exclamationmark.triangle.fill"
        case .fail: return "xmark.circle.fill"
        case .info: return "minus.circle"
        }
    }
    var tint: Color {
        switch level {
        case .ok: return .green
        case .warn: return .orange
        case .fail: return .red
        case .info: return .secondary
        }
    }
}

enum Diagnostics {
    static func checks(config: [String: Any], snap: Snapshot) -> [Check] {
        let fm = FileManager.default
        var out: [Check] = []

        let configOK = fm.fileExists(atPath: Paths.configFile.path) && !config.isEmpty
        out.append(Check(label: "Config file", detail: Paths.configFile.path,
                         level: configOK ? .ok : .fail))

        out.append(Check(label: "Core version",
                         detail: snap.version.isEmpty ? "unknown" : "v\(snap.version)",
                         level: snap.version.isEmpty ? .warn : .ok))

        // The most useful ML fact: did it fall back to Python despite native
        // being configured (missing bundle / failed native start)?
        let configuredEngine = (config["ml_engine"] as? String) ?? "native"
        if snap.mlHealthy {
            let fellBack = configuredEngine == "native" && snap.mlEngine == .python
            out.append(Check(label: "ML engine",
                             detail: fellBack ? "configured native, running Python (fallback)"
                                              : "\(snap.mlEngine.badge) — CLIP · Faces · OCR",
                             level: fellBack ? .warn : .ok))
        } else {
            out.append(Check(label: "ML engine",
                             detail: snap.mlUp ? "starting…" : "not running",
                             level: snap.mlUp ? .warn : .fail))
        }

        out.append(Check(label: "Immich",
                         detail: snap.immichURL.isEmpty ? "immich_url not set" : snap.immichURL,
                         level: snap.immichURL.isEmpty ? .fail : (snap.immichReachable ? .ok : .fail)))

        let key = config["api_key"] as? String ?? ""
        out.append(Check(label: "API key",
                         detail: key.isEmpty ? "not set (job counts disabled)"
                            : (snap.apiKeyValid ? "valid" : "set, but Immich rejected it"),
                         level: key.isEmpty ? .warn : (snap.apiKeyValid ? .ok : .fail)))

        if snap.dashboardEnabled {
            out.append(Check(label: "Dashboard",
                             detail: snap.dashboardUp ? "running on localhost:\(snap.dashboardPort)"
                                                      : "enabled, not running",
                             level: snap.dashboardUp ? .ok : .warn))
        } else {
            out.append(Check(label: "Dashboard", detail: "off", level: .info))
        }

        out.append(Check(label: "Data dir", detail: Paths.dataDir.path,
                         level: fm.fileExists(atPath: Paths.dataDir.path) ? .ok : .fail))

        return out
    }

    // Plain text for pasting into a GitHub issue. The API key is NEVER included
    // (only whether it validated). Ends with log tails, the usual culprits.
    static func copyText(config: [String: Any], snap: Snapshot) -> String {
        let appVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        var lines = [
            "Immich Accelerator diagnostics",
            "",
            "menu bar app: \(appVersion)",
            "core: \(snap.version.isEmpty ? "?" : snap.version)",
            "immich: \(snap.immichVersion.isEmpty ? "?" : snap.immichVersion)",
            "macOS: \(ProcessInfo.processInfo.operatingSystemVersionString)",
            "",
        ]
        for c in checks(config: config, snap: snap) {
            let mark: String
            switch c.level {
            case .ok: mark = "OK  "
            case .warn: mark = "WARN"
            case .fail: mark = "FAIL"
            case .info: mark = "--  "
            }
            lines.append("[\(mark)] \(c.label): \(c.detail)")
        }
        lines.append("")
        lines.append(logTail("ml.log", 30))
        lines.append("")
        lines.append(logTail("worker.log", 20))
        return lines.joined(separator: "\n")
    }

    private static func logTail(_ name: String, _ n: Int) -> String {
        let f = Paths.dataDir.appendingPathComponent("logs/\(name)")
        guard let text = try? String(contentsOf: f, encoding: .utf8) else {
            return "--- \(name): (not found) ---"
        }
        let tail = text.split(separator: "\n", omittingEmptySubsequences: false).suffix(n)
        return "--- \(name) (last \(n) lines) ---\n" + tail.joined(separator: "\n")
    }
}
