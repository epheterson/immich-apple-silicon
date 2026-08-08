import SwiftUI

/// The settings window: a sidebar and a grouped `Form`, which is what a macOS
/// settings window has looked like since Ventura replaced System Preferences.
///
/// The shape is the point. Before this it was six `GroupBox`es stacked in one
/// scrolling column with every row hand-built, which produced exactly the
/// defects hand-built rows produce: each `Toggle` sat immediately after its own
/// label, so three switches landed at three different x positions; the
/// Components box hugged its content and was visibly narrower than its
/// neighbours; and component titles used the default body font while every
/// other row used `.callout`.
///
/// A grouped `Form` inside a `NavigationSplitView` gives all of that away for
/// free: macOS owns the label column, right-aligns the controls, sizes the
/// cards and picks the fonts. That is also why it will still look current after
/// the next macOS release, instead of drifting away from the system again.
struct SettingsView: View {
    @ObservedObject var model: StatusModel

    /// One sidebar row. The tinted rounded square is not decoration: it is the
    /// thing that makes a sidebar read as macOS settings rather than as a
    /// generic list, and System Settings gives every pane one.
    private enum Pane: String, Hashable, CaseIterable, Identifiable {
        case general, components, ml, diagnostics

        // Identity is the case itself, so List's selection binding is a Pane?
        // rather than the String? an id-of-String would demand. Getting this
        // wrong still compiles, via a different List overload, and then simply
        // fails to track the selection.
        var id: Self { self }

        var title: String {
            switch self {
            case .general: return "General"
            case .components: return "Components"
            case .ml: return "Machine Learning"
            case .diagnostics: return "Diagnostics"
            }
        }
        var symbol: String {
            switch self {
            case .general: return "gearshape.fill"
            case .components: return "square.stack.3d.up.fill"
            case .ml: return "brain.head.profile"
            case .diagnostics: return "stethoscope"
            }
        }
        var tint: Color {
            switch self {
            case .general: return .gray
            case .components: return .blue
            case .ml: return .purple
            case .diagnostics: return .teal
            }
        }
    }

    // ACCEL_SETTINGS_TAB picks the opening pane, so each one can be captured
    // headlessly. Same dev affordance as ACCEL_SHOW_SETTINGS (see AppDelegate).
    @State private var pane: Pane? = Pane(
        rawValue: ProcessInfo.processInfo.environment["ACCEL_SETTINGS_TAB"] ?? ""
    ) ?? .general

    // Populated at construction (not just onAppear) so the window has real
    // values immediately and an off-screen ImageRenderer capture isn't blank.
    @State private var config: [String: Any] = StatusModel.readConfig()
    @State private var engine = "native"
    @State private var savedEngine = "native"
    @State private var revealKey = false
    @State private var applying = false
    @State private var testing = false
    @State private var workerOn = true
    @State private var mlOn = true
    @State private var dashboardOn = true
    // Which component is being applied, or nil. Doubles as the "seeding state,
    // ignore onChange" guard and as the row that shows a spinner: applying a
    // change shells out to the CLI, and the toggles must not race each other
    // into a contradictory config.
    @State private var applyingComponent: String?
    @State private var componentError: String?
    @State private var launchAtLogin = LaunchAtLogin.isEnabled

    var body: some View {
        NavigationSplitView {
            List(Pane.allCases, selection: $pane) { p in
                Label {
                    Text(p.title)
                } icon: {
                    PaneIcon(systemName: p.symbol, tint: p.tint)
                }
                .padding(.vertical, Metrics.xs)
            }
            // A plain frame, because neither form of
            // navigationSplitViewColumnWidth moved it off the split view's own
            // ~140pt default, which truncated "Machine Learning" to
            // "Machine Le...". The width is not negotiable here anyway: the
            // window is fixed, so there is nothing for the split view to
            // trade against.
            .frame(width: Metrics.settingsSidebarWidth)
            // No collapse control: a settings window with a hideable sidebar
            // can be left in a state with no way back to the other panes.
            .toolbar(removing: .sidebarToggle)
        } detail: {
            detail
                // .principal is the supported way to center content in the
                // title bar. navigationTitle lands centered on macOS 15 but
                // leading on macOS 26 (the release gate runs 26), and neither
                // setting NSWindow.title nor dropping the toolbar moved it,
                // because the split view places its own title.
                .toolbar {
                    ToolbarItem(placement: .principal) {
                        Text(pane?.title ?? "Settings").font(.headline)
                    }
                }
        }
        // Still set on the window, so the title is right in Mission Control,
        // the Window menu and any screenshot of the title bar.
        .background(WindowTitle(title: pane?.title ?? "Settings"))
        // Fixed, so switching panes doesn't resize the window under the pointer.
        .frame(width: Metrics.settingsWidth, height: Metrics.settingsHeight)
        .onAppear(perform: load)
    }

    @ViewBuilder
    private var detail: some View {
        switch pane {
        case .components: componentsTab
        case .ml: mlTab
        case .diagnostics: diagnosticsTab
        default: generalTab
        }
    }

    private func load() {
        config = StatusModel.readConfig()
        savedEngine = (config["ml_engine"] as? String) ?? "native"
        engine = savedEngine
        // Plain assignment. Seeding cannot be mistaken for a user action here
        // because the toggles act through an explicit Binding whose setter is
        // the action (see componentToggle), not through .onChange watching
        // @State. A flag-guarded .onChange looked equivalent and was not:
        // SwiftUI coalesces the set-and-clear of the guard into one update
        // pass, so onChange saw it already cleared and fired anyway. On an
        // ML-off install, merely opening this window restarted the worker.
        workerOn = StatusModel.componentEnabled("worker", config)
        mlOn = StatusModel.componentEnabled("ml", config)
        dashboardOn = StatusModel.componentEnabled("dashboard", config)
    }

    // MARK: - General

    private var generalTab: some View {
        Form {
            Section {
                LabeledContent("Accelerator") {
                    HStack(spacing: Metrics.md) {
                        Text(model.snap.overall.label)
                        StatusDot(state: model.snap.overall)
                    }
                }
                LabeledContent("Immich", value: immichSummary)
            }

            Section {
                Toggle("Launch menu bar at login", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, on in LaunchAtLogin.set(on) }
            } header: {
                Text("Startup")
            } footer: {
                // The distinction people actually get wrong: this switch is
                // about the menu bar icon, not about whether photos get
                // processed. The background service is brew's, and it runs
                // whether or not anyone is logged in.
                Text("The accelerator itself runs as a background service and is unaffected by this.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }

            Section("Software Update") {
                LabeledContent("Version", value: "v\(appVersion)")
                // Only when it disagrees. The background service follows this
                // app automatically (AppDelegate.syncCoreVersion), so in normal
                // operation these are the same number, and printing it twice
                // under two internal names ("Menu bar app", "Core") was two
                // rows saying one thing. The split is only information when
                // they have actually drifted, and then it matters a lot.
                if let drifted = driftedCoreVersion {
                    LabeledContent("Background service", value: drifted)
                }
                Button("Check for Updates…") { UpdaterModel.shared.checkForUpdates() }
            }

            Section {
                LabeledContent("Files") {
                    HStack(spacing: Metrics.md) {
                        Button("Reveal Config") { Actions.revealConfig() }
                        Button("Open Logs") { Actions.openLogs() }
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    private var immichSummary: String {
        let url = model.snap.immichURL.isEmpty ? "not configured" : model.snap.immichURL
        return model.snap.immichVersion.isEmpty ? url : "v\(model.snap.immichVersion) · \(url)"
    }

    // MARK: - Components

    /// The accelerator's three separable processes. This is as fine-grained as
    /// it gets: video, thumbnails and RAW decode all run inside the one worker,
    /// so which of those happen is Immich's job scheduler, not ours.
    private var componentsTab: some View {
        Form {
            Section {
                componentToggle("worker", $workerOn, "Worker",
                                "Thumbnails, video transcoding, metadata")
                componentToggle("ml", $mlOn, "Machine Learning",
                                "Search, faces, OCR")
                componentToggle("dashboard", $dashboardOn, "Web dashboard",
                                dashboardStatus)
            } footer: {
                // No section header. The window title already says
                // "Components", and a heading under it restating the same
                // thing in different words is just noise.
                Text("Switching a component off stops it now and keeps it off across restarts. The others keep running, and Immich carries on handling that work itself.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }

            if let componentError {
                Section {
                    Label(componentError, systemImage: "exclamationmark.triangle.fill")
                        .font(.rowDetail)
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .formStyle(.grouped)
    }

    private func componentToggle(
        _ name: String, _ binding: Binding<Bool>, _ title: String, _ caption: String
    ) -> some View {
        // The switch reads @State but writes through here, so only a real
        // interaction can trigger the CLI. Seeding assigns to the @State
        // directly and is structurally incapable of firing this.
        let action = Binding<Bool>(
            get: { binding.wrappedValue },
            set: { on in
                guard applyingComponent == nil else { return }
                binding.wrappedValue = on           // optimistic, reverted below
                applyingComponent = name
                Task {
                    let result = await Actions.setComponent(name, on)
                    await model.refresh()
                    // Never leave a switch claiming something the accelerator
                    // did not do: put it back and say why.
                    if !result.ok {
                        componentError = result.message
                        binding.wrappedValue = !on  // no re-entry: setter unused
                    } else {
                        componentError = nil
                        config = StatusModel.readConfig()
                    }
                    applyingComponent = nil
                }
            })

        // Toggle owns the whole row: in a grouped Form macOS puts the label at
        // the leading edge and the switch at the trailing edge, so the three
        // switches line up regardless of how long their labels are.
        return Toggle(isOn: action) {
            VStack(alignment: .leading, spacing: Metrics.xs) {
                HStack(spacing: Metrics.md) {
                    Text(title)
                    // Turning the worker on runs a full start (extract, verify
                    // sharp, preflight) and can take minutes. A row that just
                    // went dead with no explanation reads as a hang.
                    if applyingComponent == name {
                        ProgressView().controlSize(.small)
                        Text(binding.wrappedValue ? "Starting…" : "Stopping…")
                            .font(.rowDetail).foregroundStyle(.secondary)
                    }
                }
                Text(caption).font(.rowDetail).foregroundStyle(.secondary)
            }
        }
        .toggleStyle(.switch)
        .disabled(applyingComponent != nil)
    }

    // Live state from the probe (not the toggle): reflects whether it actually
    // came up, and dodges an OrbStack port collision.
    private var dashboardStatus: String {
        if !model.snap.dashboardEnabled { return "Off" }
        return model.snap.dashboardUp
            ? "Running on localhost:\(model.snap.dashboardPort)" : "Starting…"
    }

    // MARK: - Machine Learning

    private var mlTab: some View {
        Form {
            Section {
                Picker("Engine", selection: $engine) {
                    Text("Native (Swift)").tag("native")
                    Text("Python (venv)").tag("python")
                }
                .pickerStyle(.segmented)
                LabeledContent("Running") {
                    if model.snap.mlUp {
                        BadgeLabel(text: model.snap.mlEngine.badge,
                                   tint: model.snap.mlEngine == .native ? .green : .orange)
                    } else {
                        Text(model.snap.mlEnabled ? "Stopped" : "Off")
                            .foregroundStyle(.secondary)
                    }
                }
                LabeledContent("Port", value: str("ml_port"))
            } footer: {
                if engine != savedEngine {
                    HStack(spacing: Metrics.md) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                        Text("Restarts the accelerator to take effect.")
                            .font(.rowDetail).foregroundStyle(.secondary)
                        Spacer()
                        Button(applying ? "Applying…" : "Apply") {
                            applying = true
                            Task {
                                await Actions.setMLEngine(engine)
                                savedEngine = engine
                                await model.refresh()
                                applying = false
                            }
                        }
                        .disabled(applying)
                    }
                }
            }

            Section {
                LabeledContent("Self-test") {
                    Button(testing ? "Running…" : "Run") { runMLTest() }
                        .disabled(testing || !model.snap.mlHealthy)
                }
                if let result = model.lastMLTest {
                    let passed = result.contains("OK") || result.contains("passed")
                    Label(result, systemImage: passed
                          ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundStyle(passed ? .green : .red)
                        .font(.rowDetail)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } footer: {
                Text("Sends a real image through CLIP, face detection and OCR, and reports what came back.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    private func runMLTest() {
        guard model.snap.mlHealthy, !testing else { return }
        testing = true
        Task {
            model.lastMLTest = await Actions.mlTest()
            testing = false
        }
    }

    // MARK: - Diagnostics

    private var diagnosticsTab: some View {
        Form {
            Section("Configuration") {
                ForEach(Diagnostics.checks(config: config, snap: model.snap)) { c in
                    LabeledContent {
                        Text(c.detail).textSelection(.enabled)
                            .foregroundStyle(c.level == .fail ? .primary : .secondary)
                            // Long paths belong on two lines, not truncated to
                            // uselessness: this list exists to be read and
                            // pasted into an issue.
                            .fixedSize(horizontal: false, vertical: true)
                            .multilineTextAlignment(.trailing)
                    } label: {
                        HStack(spacing: Metrics.md) {
                            Image(systemName: c.iconName)
                                .symbolRenderingMode(.hierarchical)
                                .foregroundStyle(c.tint)
                                .frame(width: Metrics.iconColumn, alignment: .center)
                            Text(c.label)
                        }
                    }
                }
            }

            Section("Credentials") {
                LabeledContent("API key") {
                    HStack(spacing: Metrics.md) {
                        // Distinguish a genuinely-missing key (breaks job counts
                        // and authenticated calls) from a present-but-hidden one.
                        Text(apiKey.isEmpty ? "not set"
                             : (revealKey ? apiKey : String(repeating: "•", count: 24)))
                            .font(.system(.callout, design: .monospaced))
                            .foregroundStyle(apiKey.isEmpty ? .secondary : .primary)
                            .textSelection(.enabled)
                        if !apiKey.isEmpty {
                            Button {
                                revealKey.toggle()
                            } label: { Image(systemName: revealKey ? "eye.slash" : "eye") }
                                .buttonStyle(.borderless)
                        }
                    }
                }
            }

            Section {
                LabeledContent("Issue report") {
                    Button {
                        Actions.copyToPasteboard(
                            Diagnostics.copyText(config: config, snap: model.snap))
                    } label: {
                        Label("Copy", systemImage: "doc.on.clipboard")
                    }
                }
            } footer: {
                Text("Copies versions, this checklist and recent log lines. Never the API key.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    // MARK: - helpers

    private var apiKey: String { config["api_key"] as? String ?? "" }

    private var appVersion: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
    }

    /// The installed core's version, but only when it is worth mentioning:
    /// different from this app's, or unreadable. nil means "in lockstep, say
    /// nothing".
    private var driftedCoreVersion: String? {
        let core = model.snap.version
        if core.isEmpty { return "unknown" }
        return core == appVersion ? nil : "v\(core)"
    }

    private func str(_ key: String) -> String {
        if let s = config[key] as? String { return s }
        if let i = config[key] as? Int { return String(i) }
        return "-"
    }
}
