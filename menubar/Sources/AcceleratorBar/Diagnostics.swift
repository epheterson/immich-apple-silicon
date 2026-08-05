import SwiftUI

// A single validated configuration/health fact, shown in Settings and included
// in the copy-for-issue text. Read-only; computed from config + the live
// snapshot + a couple of cheap filesystem checks (no blocking work here, the
// network-derived facts come pre-probed off the snapshot).
struct Check: Identifiable {
    enum Level { case ok, warn, fail, info }
    // The label is the stable identity. A fresh UUID per rebuild would give
    // SwiftUI a whole new set of rows on every status poll, tearing down the
    // list and wiping any in-progress text selection every few seconds.
    var id: String { label }
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
                                              : "\(snap.mlEngine.badge): CLIP · Faces · OCR",
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
        if key.isEmpty {
            out.append(Check(label: "API key", detail: "not set (job counts disabled)",
                             level: .warn))
        } else {
            // The key is only exercised while the worker is up, so an unchecked
            // key reads as "set", never as rejected.
            switch snap.apiKeyValid {
            case .some(true):
                out.append(Check(label: "API key", detail: "valid", level: .ok))
            case .some(false):
                out.append(Check(label: "API key", detail: "set, but Immich rejected it",
                                 level: .fail))
            case .none:
                out.append(Check(label: "API key",
                                 detail: "set (not checked while the worker is stopped)",
                                 level: .info))
            }
        }

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

    // Read only the end of the file. Service logs are capped at 200 MB, so
    // slurping the whole thing to keep 30 lines would stall the UI and spike
    // memory by hundreds of MB. Seek to a small window off the end instead.
    private static func logTail(_ name: String, _ n: Int) -> String {
        let f = Paths.dataDir.appendingPathComponent("logs/\(name)")
        let header = "--- \(name) (last \(n) lines) ---\n"
        guard let handle = try? FileHandle(forReadingFrom: f) else {
            return "--- \(name): (not found) ---"
        }
        defer { try? handle.close() }

        // 256 KB comfortably covers n lines of these logs, including stack traces.
        let window = 256 * 1024
        guard let end = try? handle.seekToEnd() else { return header }
        let start = end > UInt64(window) ? end - UInt64(window) : 0
        try? handle.seek(toOffset: start)
        guard let data = try? handle.readToEnd(),
              let text = String(data: data, encoding: .utf8)
                ?? String(data: data, encoding: .isoLatin1)
        else { return header }

        var lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        // The first line is probably truncated mid-way by the seek; drop it.
        if start > 0, !lines.isEmpty { lines.removeFirst() }
        return header + lines.suffix(n).joined(separator: "\n")
    }
}
