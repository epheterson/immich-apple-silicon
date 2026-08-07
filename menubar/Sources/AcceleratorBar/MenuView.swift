import SwiftUI

// The dropdown panel. One glance = full picture; one click = any daily action.
struct MenuView: View {
    @ObservedObject var model: StatusModel
    @State private var launchAtLogin = LaunchAtLogin.isEnabled
    @State private var testing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            InsetDivider()
            statusRows
            InsetDivider()
            actions
            InsetDivider()
            footer
        }
        .frame(width: Metrics.panelWidth)
        .onAppear { model.startPolling(interval: 3) }
        .onDisappear { model.startPolling(interval: 15) }
    }

    private var header: some View {
        HStack(spacing: Metrics.md) {
            Image(systemName: "bolt.fill")
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(model.snap.overall == .running ? Color.accentColor : .secondary)
                .font(.title3)
                .frame(width: Metrics.iconColumn, alignment: .center)
            VStack(alignment: .leading, spacing: Metrics.xs) {
                Text("Immich Accelerator").font(.headline)
                Text(subtitle).font(.rowDetail).foregroundStyle(.secondary)
            }
            Spacer(minLength: Metrics.md)
            // One state signal, not two. The bolt tints with health already;
            // a dot beside it repeated the same fact in a second vocabulary.
            Text(model.snap.overall.label)
                .font(.badge)
                .foregroundStyle(model.snap.overall == .running ? .secondary : .primary)
            StatusDot(state: model.snap.overall)
        }
        .panelGutter()
        .padding(.vertical, Metrics.lg)
    }

    private var subtitle: String {
        var parts: [String] = []
        if !model.snap.version.isEmpty { parts.append("v\(model.snap.version)") }
        if !model.snap.immichVersion.isEmpty { parts.append("Immich \(model.snap.immichVersion)") }
        return parts.isEmpty ? model.snap.overall.label : parts.joined(separator: "  ·  ")
    }

    // One rule for all three: a row exists only for a component the user turned
    // on. Disabled and "not running" are different facts, and showing a
    // permanent red row for something switched off deliberately trains people
    // to ignore the colors.
    private var statusRows: some View {
        VStack(spacing: Metrics.xs) {
            if model.snap.workerEnabled {
                StatusRow(
                    icon: "gearshape.2.fill", name: "Worker",
                    detail: workerDetail, ok: model.snap.workerUp)
            }
            if model.snap.mlEnabled {
                StatusRow(
                    icon: "brain.fill", name: "Machine Learning",
                    detail: mlDetail, ok: model.snap.mlHealthy,
                    badge: model.snap.mlUp ? model.snap.mlEngine.badge : nil,
                    badgeTint: model.snap.mlEngine == .native ? .green : .orange)
                    .contentShape(Rectangle())
                    .onTapGesture { runMLTest() }
                    .help("Click to run an ML self-test")
            }
            // Per-queue progress, when the dashboard is serving it. Only the
            // queues with work left: five bars pinned at 100% is furniture, and
            // the interesting question is always what is still outstanding.
            if model.snap.workerEnabled {
                let pending = model.snap.queues.filter { !$0.complete }
                if !pending.isEmpty {
                    VStack(spacing: Metrics.sm) {
                        ForEach(pending) { q in QueueRow(queue: q) }
                    }
                    .padding(.leading, Metrics.iconColumn + Metrics.md)
                    .padding(.trailing, Metrics.rowPadV)
                    .padding(.top, Metrics.xs)
                    .padding(.bottom, Metrics.sm)
                }
            }
            if model.snap.dashboardEnabled {
                StatusRow(
                    icon: "gauge.with.dots.needle.50percent", name: "Dashboard",
                    detail: model.snap.dashboardUp ? "localhost:\(model.snap.dashboardPort)" : "Not running",
                    ok: model.snap.dashboardUp)
            }
            if !model.snap.workerEnabled && !model.snap.mlEnabled && !model.snap.dashboardEnabled {
                Text("Every component is switched off.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, Metrics.rowPadV)
                    .padding(.vertical, Metrics.sm)
            }
        }
        .padding(.horizontal, Metrics.md)
        .padding(.vertical, Metrics.sm)
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
        VStack(spacing: Metrics.md) {
            HStack(spacing: Metrics.md) {
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
                HStack(spacing: Metrics.sm) {
                    Image(systemName: result.contains("OK") || result.contains("passed")
                          ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundStyle(result.contains("OK") || result.contains("passed")
                                         ? .green : .red)
                    Text(result).font(.caption)
                    Spacer()
                }
                .padding(.horizontal, Metrics.xs)
                .transition(.opacity)
            }
        }
        .panelGutter()
        .padding(.vertical, Metrics.md)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 0) {
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
            Divider().padding(.vertical, Metrics.sm).padding(.horizontal, Metrics.sm)
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
            Divider().padding(.vertical, Metrics.sm).padding(.horizontal, Metrics.sm)
            // Full-width settings row so it lines up with the link rows above
            // instead of a narrow checkbox centering itself in the panel.
            HStack(spacing: Metrics.md) {
                RowIcon(systemName: "arrow.up.forward.app")
                Text("Launch at Login").font(.callout)
                Spacer()
                Toggle("", isOn: $launchAtLogin)
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .controlSize(.mini)
            }
            .padding(.horizontal, Metrics.md)
            .padding(.vertical, Metrics.rowPadV)
            .onChange(of: launchAtLogin) { _, on in LaunchAtLogin.set(on) }
            LinkRow(icon: "power", title: "Quit") { NSApp.terminate(nil) }
        }
        .padding(.horizontal, Metrics.md)
        .padding(.vertical, Metrics.md)
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
        Circle().fill(color).frame(width: Metrics.dot, height: Metrics.dot)
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
        HStack(spacing: Metrics.md) {
            RowIcon(systemName: icon, active: ok)
            VStack(alignment: .leading, spacing: Metrics.xs) {
                HStack(spacing: Metrics.sm) {
                    Text(name).font(.rowTitle)
                    if let badge {
                        BadgeLabel(text: badge, tint: badgeTint)
                    }
                }
                Text(detail).font(.rowDetail).foregroundStyle(.secondary)
            }
            Spacer(minLength: Metrics.md)
            StatusDot(state: ok ? .running : .stopped)
        }
        .padding(.horizontal, Metrics.rowPadV)
        .padding(.vertical, Metrics.rowPadV)
    }
}

/// One queue's completion: name, a thin bar, and what is left.
///
/// Indented under the Worker row rather than given its own icon, because these
/// are that row's detail, not five more services.
struct QueueRow: View {
    let queue: QueueProgress

    var body: some View {
        HStack(spacing: Metrics.md) {
            Text(queue.label)
                .font(.rowDetail)
                .foregroundStyle(.secondary)
                .frame(width: 64, alignment: .leading)
            ProgressView(value: queue.fraction)
                .progressViewStyle(.linear)
                .controlSize(.small)
            Text("\(queue.total - queue.done)")
                .font(.rowDetail)
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(minWidth: 34, alignment: .trailing)
        }
        .help("\(queue.done) of \(queue.total) done")
    }
}

/// The small capsule beside a row title (NATIVE / PYTHON). Its own type so the
/// shape, tint opacity and padding are stated once.
struct BadgeLabel: View {
    let text: String
    var tint: Color = .green

    var body: some View {
        Text(text)
            .font(.badge)
            .padding(.horizontal, Metrics.sm)
            .padding(.vertical, 1)
            .background(tint.opacity(0.18), in: Capsule())
            .foregroundStyle(tint)
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

    @State private var hovering = false

    var body: some View {
        Button {
            action()
            // Clicking a link dismisses the panel like a normal menu item.
            WindowManager.shared.dismissMenuBarPanel()
        } label: {
            HStack(spacing: Metrics.md) {
                RowIcon(systemName: icon, active: hovering)
                Text(title).font(.callout)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Metrics.rowPadV)
            .padding(.vertical, Metrics.sm)
            .contentShape(Rectangle())
            // A row that highlights under the pointer is what makes this read
            // as a menu rather than a list of labels. Without it the panel is
            // the one part of the app that does not respond to the cursor.
            .background(
                RoundedRectangle(cornerRadius: Metrics.rowRadius, style: .continuous)
                    .fill(Color.primary.opacity(hovering ? 0.08 : 0))
            )
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}
