import SwiftUI

// The dropdown panel. One glance = full picture; one click = any daily action.
struct MenuView: View {
    @ObservedObject var model: StatusModel
    @State private var launchAtLogin = LaunchAtLogin.isEnabled
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
                detail: workerDetail, ok: model.snap.workerUp)
            StatusRow(
                icon: "brain.fill", name: "Machine Learning",
                detail: mlDetail, ok: model.snap.mlHealthy,
                badge: model.snap.mlUp ? model.snap.mlEngine.badge : nil,
                badgeTint: model.snap.mlEngine == .native ? .green : .orange)
                .contentShape(Rectangle())
                .onTapGesture { runMLTest() }
                .help("Click to run an ML self-test")
            // Only show the dashboard row when it's enabled; a user who turned
            // it off shouldn't see a permanent red "Not running".
            if model.snap.dashboardEnabled {
                StatusRow(
                    icon: "gauge.with.dots.needle.50percent", name: "Dashboard",
                    detail: model.snap.dashboardUp ? "localhost:\(model.snap.dashboardPort)" : "Not running",
                    ok: model.snap.dashboardUp)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
    }

    private var mlDetail: String {
        if testing { return "Testing…" }
        // A model fetch takes minutes on the big models and makes Immich's jobs
        // fail meanwhile, so say so rather than looking merely slow.
        if model.snap.downloadTotal > 0 {
            let pct = model.snap.downloadDone * 100 / max(model.snap.downloadTotal, 1)
            return "Downloading model… \(pct)%"
        }
        if model.snap.mlHealthy { return "CLIP · Faces · OCR" }
        if model.snap.mlUp { return "Starting…" }
        return "Not running"
    }

    private func runMLTest() {
        guard model.snap.mlHealthy, !testing else { return }
        testing = true
        Task {
            model.lastMLTest = await Actions.mlTest()
            testing = false
        }
    }

    private var workerDetail: String {
        guard model.snap.workerUp else { return "Not running" }
        let active = model.snap.jobsActive, waiting = model.snap.jobsWaiting
        if active == 0 && waiting == 0 { return "Idle" }
        if waiting == 0 { return "\(active) processing" }
        return "\(active) processing · \(waiting) queued"
    }

    private var actions: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                if model.snap.overall == .stopped {
                    ActionButton(title: "Start", icon: "play.fill", prominent: true) {
                        await Actions.startService(); await model.refresh()
                    }
                } else {
                    ActionButton(title: "Stop", icon: "stop.fill") {
                        await Actions.stopService(); await model.refresh()
                    }
                    ActionButton(title: "Restart", icon: "arrow.clockwise") {
                        await Actions.restartService(); await model.refresh()
                    }
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
        VStack(alignment: .leading, spacing: 2) {
            LinkRow(icon: "photo.on.rectangle.angled", title: "Open Immich") {
                Actions.openImmich(model.snap.openImmichURL)
            }
            // Only offer this when the dashboard is actually up: if the port is
            // held by something else (a container proxy, say), opening it would
            // navigate to that other app instead.
            if model.snap.dashboardUp {
                LinkRow(icon: "gauge.with.dots.needle.50percent", title: "Open Dashboard") {
                    Actions.openDashboard(port: model.snap.dashboardPort)
                }
            }
            LinkRow(icon: "doc.text.magnifyingglass", title: "Open Logs") {
                Actions.openLogs()
            }
            Divider().padding(.vertical, 4).padding(.horizontal, 4)
            if Paths.isConfigured {
                LinkRow(icon: "slider.horizontal.3", title: "Settings…") {
                    WindowManager.shared.showSettings(model: model)
                }
            } else {
                LinkRow(icon: "wand.and.stars", title: "Set Up Accelerator…") {
                    WindowManager.shared.showOnboarding(model: model)
                }
            }
            LinkRow(icon: "arrow.down.circle", title: "Check for Updates…") {
                UpdaterModel.shared.checkForUpdates()
            }
            Divider().padding(.vertical, 4).padding(.horizontal, 4)
            // Full-width settings row so it lines up with the link rows above
            // instead of a narrow checkbox centering itself in the panel.
            HStack(spacing: 10) {
                Image(systemName: "arrow.up.forward.app")
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(.secondary)
                    .frame(width: 20)
                Text("Launch at Login").font(.callout)
                Spacer()
                Toggle("", isOn: $launchAtLogin)
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .controlSize(.mini)
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .onChange(of: launchAtLogin) { _, on in LaunchAtLogin.set(on) }
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
        Button {
            action()
            // Clicking a link dismisses the panel like a normal menu item.
            WindowManager.shared.dismissMenuBarPanel()
        } label: {
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
