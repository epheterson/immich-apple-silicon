import SwiftUI
import ServiceManagement

// The dropdown panel. One glance = full picture; one click = any daily action.
struct MenuView: View {
    @ObservedObject var model: StatusModel
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled
    @State private var testing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().padding(.horizontal, 12)
            statusRows
            Divider().padding(.horizontal, 12)
            actions
            Divider().padding(.horizontal, 12)
            footer
        }
        .frame(width: 300)
        .onAppear { model.startPolling(interval: 3) }
        .onDisappear { model.startPolling(interval: 15) }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "bolt.fill")
                .foregroundStyle(model.snap.overall == .running ? .yellow : .secondary)
                .font(.title3)
            VStack(alignment: .leading, spacing: 1) {
                Text("Immich Accelerator").font(.headline)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            StatusDot(state: model.snap.overall).scaleEffect(1.25)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }

    private var subtitle: String {
        var parts: [String] = []
        if !model.snap.version.isEmpty { parts.append("v\(model.snap.version)") }
        if !model.snap.immichVersion.isEmpty { parts.append("Immich \(model.snap.immichVersion)") }
        return parts.isEmpty ? model.snap.overall.label : parts.joined(separator: "  ·  ")
    }

    private var statusRows: some View {
        VStack(spacing: 2) {
            StatusRow(
                icon: "gearshape.2.fill", name: "Worker",
                detail: model.snap.workerUp ? "Processing jobs" : "Not running",
                ok: model.snap.workerUp)
            StatusRow(
                icon: "brain.fill", name: "Machine Learning",
                detail: mlDetail, ok: model.snap.mlHealthy,
                badge: model.snap.mlUp ? model.snap.mlEngine.badge : nil,
                badgeTint: model.snap.mlEngine == .native ? .green : .orange)
            StatusRow(
                icon: "gauge.with.dots.needle.50percent", name: "Dashboard",
                detail: model.snap.dashboardUp ? "localhost:8420" : "Not running",
                ok: model.snap.dashboardUp)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
    }

    private var mlDetail: String {
        if model.snap.mlHealthy { return "CLIP · Faces · OCR" }
        if model.snap.mlUp { return "Starting…" }
        return "Not running"
    }

    private var actions: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                if model.snap.overall == .stopped {
                    ActionButton(title: "Start", icon: "play.fill", prominent: true) {
                        await Actions.startService(); await model.refresh()
                    }
                } else {
                    ActionButton(title: "Restart", icon: "arrow.clockwise") {
                        await Actions.restartService(); await model.refresh()
                    }
                    ActionButton(title: "Stop", icon: "stop.fill") {
                        await Actions.stopService(); await model.refresh()
                    }
                }
                ActionButton(title: testing ? "Testing…" : "Test ML",
                             icon: "checkmark.seal", disabled: testing || !model.snap.mlHealthy) {
                    testing = true
                    model.lastMLTest = await Actions.mlTest()
                    testing = false
                }
            }
            if let result = model.lastMLTest {
                HStack(spacing: 5) {
                    Image(systemName: result.contains("OK") || result.contains("passed")
                          ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundStyle(result.contains("OK") || result.contains("passed")
                                         ? .green : .red)
                    Text(result).font(.caption)
                    Spacer()
                }
                .padding(.horizontal, 4)
                .transition(.opacity)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var footer: some View {
        VStack(spacing: 2) {
            LinkRow(icon: "photo.on.rectangle.angled", title: "Open Immich") {
                Actions.openImmich(model.snap.immichURL)
            }
            LinkRow(icon: "gauge.with.dots.needle.50percent", title: "Open Dashboard") {
                Actions.openDashboard()
            }
            LinkRow(icon: "doc.text.magnifyingglass", title: "Open Logs") {
                Actions.openLogs()
            }
            Divider().padding(.vertical, 4).padding(.horizontal, 4)
            Toggle(isOn: $launchAtLogin) {
                Text("Launch at Login").font(.callout)
            }
            .toggleStyle(.checkbox)
            .padding(.horizontal, 6)
            .onChange(of: launchAtLogin) { _, on in
                try? on ? SMAppService.mainApp.register() : SMAppService.mainApp.unregister()
            }
            LinkRow(icon: "power", title: "Quit") { NSApp.terminate(nil) }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 8)
    }
}

// MARK: - components

struct StatusDot: View {
    let state: ServiceState
    var color: Color {
        switch state {
        case .running: return .green
        case .stopped: return .secondary.opacity(0.5)
        case .degraded: return .orange
        }
    }
    var body: some View {
        Circle().fill(color).frame(width: 9, height: 9)
            .shadow(color: color.opacity(0.5), radius: 3)
    }
}

struct StatusRow: View {
    let icon: String
    let name: String
    let detail: String
    let ok: Bool
    var badge: String? = nil
    var badgeTint: Color = .green

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(ok ? Color.accentColor : .secondary)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(name).font(.callout).fontWeight(.medium)
                    if let badge {
                        Text(badge)
                            .font(.system(size: 9, weight: .semibold, design: .rounded))
                            .padding(.horizontal, 5).padding(.vertical, 1.5)
                            .background(badgeTint.opacity(0.18), in: Capsule())
                            .foregroundStyle(badgeTint)
                    }
                }
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            StatusDot(state: ok ? .running : .stopped)
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 5)
    }
}

struct ActionButton: View {
    let title: String
    let icon: String
    var prominent = false
    var disabled = false
    let action: () async -> Void

    var body: some View {
        Button {
            Task { await action() }
        } label: {
            Label(title, systemImage: icon)
                .font(.callout)
                .frame(maxWidth: .infinity)
        }
        .controlSize(.regular)
        .buttonStyle(.bordered)
        .tint(prominent ? .accentColor : nil)
        .disabled(disabled)
    }
}

struct LinkRow: View {
    let icon: String
    let title: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(.secondary)
                    .frame(width: 20)
                Text(title).font(.callout)
                Spacer()
            }
            .contentShape(Rectangle())
            .padding(.horizontal, 6)
            .padding(.vertical, 4)
        }
        .buttonStyle(.plain)
    }
}
