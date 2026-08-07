import SwiftUI

/// The settings window.
///
/// Four tabs, each a grouped `Form`. That structure is doing real work, not
/// decoration. The previous version stacked six `GroupBox`es in one scrolling
/// column and hand-built every row, which produced exactly the defects you get
/// from hand-building rows: each `Toggle` sat immediately after its own label,
/// so three switches landed at three different x positions; the Components box
/// hugged its content and was visibly narrower than its neighbours because
/// nothing inside it was full width; and the component titles used the default
/// body font while every other row used `.callout`, so they read oversized.
///
/// A grouped `Form` gives all of that away for free. macOS owns the label
/// column, right-aligns the controls, sizes the cards to the window and picks
/// the fonts, which is also why it will keep matching System Settings after the
/// next macOS release instead of drifting away from it.
struct SettingsView: View {
    @ObservedObject var model: StatusModel

    private enum Tab: Hashable { case general, components, ml, diagnostics }
    @State private var tab: Tab = .general

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
        TabView(selection: $tab) {
            generalTab
                .tabItem { Label("General", systemImage: "gearshape") }
                .tag(Tab.general)
            componentsTab
                .tabItem { Label("Components", systemImage: "square.stack.3d.up") }
                .tag(Tab.components)
            mlTab
                .tabItem { Label("Machine Learning", systemImage: "brain") }
                .tag(Tab.ml)
            diagnosticsTab
                .tabItem { Label("Diagnostics", systemImage: "stethoscope") }
                .tag(Tab.diagnostics)
        }
        // Fixed, so switching tabs doesn't resize the window under the pointer.
        // Sized to the tallest tab (Diagnostics) rather than letting each tab
        // pick its own height.
        .frame(width: Metrics.settingsWidth, height: Metrics.settingsHeight)
        .onAppear(perform: load)
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
                LabeledContent("Menu bar app", value: "v\(appVersion)")
                LabeledContent("Core", value: model.snap.version.isEmpty
                               ? "unknown" : "v\(model.snap.version)")
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
            } header: {
                Text("What this Mac runs")
            } footer: {
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
            } header: {
                Text("Engine")
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

    private func str(_ key: String) -> String {
        if let s = config[key] as? String { return s }
        if let i = config[key] as? Int { return String(i) }
        return "-"
    }
}
