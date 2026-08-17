import SwiftUI

// The dropdown panel. One glance = full picture; one click = any daily action.
struct MenuView: View {
    @ObservedObject var model: StatusModel
    @State private var launchAtLogin = LaunchAtLogin.isEnabled
    @State private var testing = false
    /// The queue rows on screen. Which rows exist is decided once per opening
    /// and then frozen; only their values track the model. See syncQueueRows.
    @State private var queueRows: [QueueProgress] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            InsetDivider()
            actions
            InsetDivider()
            footer
        }
        .frame(width: Metrics.panelWidth)
        .onAppear {
            // Order matters: ask for queue detail before the poll starts, or
            // the first pass skips it and the rows arrive a cycle late.
            model.wantsQueueDetail = true
            model.startPolling(interval: 3)
            syncQueueRows(model.snap.queues)
        }
        .onChange(of: model.snap.queues) { _, new in syncQueueRows(new) }
        .onDisappear {
            model.wantsQueueDetail = false
            model.startPolling(interval: 15)
            // Re-decide on the next opening, so a queue that finished while the
            // panel was shut stops taking up a row.
            queueRows = []
        }
    }

    /// Update the queue rows, holding their membership steady while the panel
    /// is open.
    ///
    /// A menu that resizes while the pointer is in it is the single worst thing
    /// a panel like this can do, and this one had two ways to do it: a queue
    /// crossing 100% dropped its row, and any slow dashboard poll dropped all
    /// of them at once. The second is fixed at the source (StatusModel keeps
    /// the last good answer when it could not ask); this handles the first, and
    /// makes the panel's height a function of when you opened it rather than of
    /// what finished while you were reading.
    ///
    /// "Steady" is not "frozen". An empty array is StatusModel saying the
    /// dashboard is gone, not saying nothing came back, and a row whose queue
    /// stops being reported has no live number behind it. Both must clear, or
    /// the panel keeps showing counts nothing is refreshing, which is the same
    /// lie as the resize, only quieter.
    private func syncQueueRows(_ incoming: [QueueProgress]) {
        guard !incoming.isEmpty else {
            queueRows = []
            return
        }
        let latest = Dictionary(incoming.map { ($0.key, $0) }, uniquingKeysWith: { a, _ in a })
        guard !queueRows.isEmpty else {
            queueRows = incoming.filter { !$0.complete }
            return
        }
        queueRows = queueRows.compactMap { latest[$0.key] }
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
            StatusDot(state: model.snap.overall)
        }
        .panelGutter()
        .padding(.vertical, Metrics.lg)
    }

    /// One line, and it is the state. This used to prefer version numbers and
    /// fall back to the state only when it had none, while a badge on the right
    /// showed the state as well — so a stopped accelerator said "Stopped"
    /// twice in one row, and a running one spent its only line of prose on
    /// build numbers nobody opened the panel to read.
    private var subtitle: String {
        if model.snap.overall != .running { return model.snap.overall.label }
        if let p = overallProgress { return p }
        return "Running"
    }

    /// What is left to do, in one number. Five per-queue bars competed with the
    /// dashboard, which does that job better and on a phone, and made the
    /// panel's height depend on what happened to be running.
    private var overallProgress: String? {
        let live = model.snap.queues.filter { !$0.complete }
        guard !live.isEmpty else { return nil }
        let remaining = live.reduce(0) { $0 + max($1.remaining, 0) }
        guard remaining > 0 else { return nil }
        return "\(remaining.formatted()) to process"
    }

    // One rule for all three: a row exists only for a component the user turned
    // on. Disabled and "not running" are different facts, and showing a
    // permanent red row for something switched off deliberately trains people
    // to ignore the colors.


    private func runMLTest() {
        guard model.snap.mlHealthy, !testing else { return }
        testing = true
        Task {
            model.lastMLTest = await Actions.mlTest()
            testing = false
        }
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

    /// Two destinations, one door to everything else, and out.
    ///
    /// Open Logs, Check for Updates and a Launch at Login switch were all here
    /// too. Each already exists in Settings, and the login switch had become a
    /// second, differently-worded copy of a control that is now two controls
    /// there — so the panel was quietly disagreeing with Settings about what it
    /// even does.
    private var footer: some View {
        VStack(alignment: .leading, spacing: 0) {
            LinkRow(icon: "photo.on.rectangle.angled", title: "Open Immich") {
                Actions.openImmich(model.snap.openImmichURL)
            }
            // Only when it is actually up: if the port is held by something
            // else, this would open that instead.
            if model.snap.dashboardUp {
                LinkRow(icon: "gauge.with.dots.needle.50percent", title: "Open Dashboard") {
                    Actions.openDashboard(port: model.snap.dashboardPort)
                }
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

    var body: some View {
        HStack(spacing: Metrics.md) {
            RowIcon(systemName: icon, active: ok)
            VStack(alignment: .leading, spacing: Metrics.xs) {
                Text(name).font(.rowTitle)
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
            // Grouped, because six-figure backlogs are normal here and
            // "106220" is a number you have to count digits on.
            Text(queue.remaining, format: .number.grouping(.automatic))
                .font(.rowDetail)
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(minWidth: 52, alignment: .trailing)
        }
        .help("\(queue.done.formatted()) of \(queue.total.formatted()) done")
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
