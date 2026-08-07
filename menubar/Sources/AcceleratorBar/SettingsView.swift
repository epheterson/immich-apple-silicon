import SwiftUI

// A compact settings/info window: see how the accelerator is wired, switch the
// ML engine, and reach the config and logs. Read-mostly; the one mutation
// (engine switch) is explicit and warns that it restarts the service.
struct SettingsView: View {
    @ObservedObject var model: StatusModel
    @AppStorage("hasOnboarded") private var hasOnboarded = false

    // Populated at construction (not just onAppear) so the window has real
    // values immediately and an off-screen ImageRenderer capture isn't blank.
    @State private var config: [String: Any] = StatusModel.readConfig()
    @State private var engine = "native"
    @State private var savedEngine = "native"
    @State private var revealKey = false
    @State private var applying = false
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
        VStack(alignment: .leading, spacing: Metrics.xl) {
            mlSection
            componentsSection
            configSection
            keySection
            updatesSection
            Divider()
            footer
        }
        .padding(Metrics.xxl)
        .frame(width: Metrics.settingsWidth)
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

    // MARK: - sections

    private var configSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: Metrics.rowPadV) {
                ForEach(Diagnostics.checks(config: config, snap: model.snap)) { c in
                    HStack(alignment: .firstTextBaseline, spacing: Metrics.md) {
                        Image(systemName: c.iconName)
                            .symbolRenderingMode(.hierarchical)
                            .foregroundStyle(c.tint)
                            .frame(width: Metrics.iconColumn, alignment: .center)
                        Text(c.label).foregroundStyle(.secondary)
                            .frame(width: 96, alignment: .leading)
                        Text(c.detail).textSelection(.enabled)
                            .foregroundStyle(c.level == .fail ? .primary : .secondary)
                            // Long paths belong on two lines, not truncated to
                            // uselessness: this list exists to be read and
                            // pasted into an issue.
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }
                    .font(.callout)
                }
                HStack {
                    Spacer()
                    Button {
                        Actions.copyToPasteboard(
                            Diagnostics.copyText(config: config, snap: model.snap))
                    } label: {
                        Label("Copy for issue report", systemImage: "doc.on.clipboard")
                    }
                    .controlSize(.small)
                    .help("Copies versions, this checklist, and recent log lines. No API key.")
                }
            }
            .padding(Metrics.sm)
        } label: { Label("Configuration", systemImage: "checklist") }
    }

    private var mlSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: Metrics.lg) {
                Picker("Engine", selection: $engine) {
                    Text("Native (Swift)").tag("native")
                    Text("Python (venv)").tag("python")
                }
                .pickerStyle(.segmented)
                row("Running", model.snap.mlUp ? model.snap.mlEngine.badge : "stopped")
                row("Port", str("ml_port"))
                if engine != savedEngine {
                    HStack(spacing: 8) {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                        Text("Restarts the accelerator to take effect.")
                            .font(.caption).foregroundStyle(.secondary)
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
            .padding(Metrics.sm)
        } label: { Label("Machine Learning", systemImage: "brain.fill") }
    }

    // The accelerator's three separable processes. This is as fine-grained as
    // it gets: video, thumbnails and RAW decode all run inside the one worker,
    // so which of those happen is Immich's job scheduler, not ours.
    private var componentsSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: Metrics.lg) {
                componentToggle("worker", $workerOn, "Worker",
                                "Thumbnails, video transcoding, metadata")
                componentToggle("ml", $mlOn, "Machine Learning",
                                "Search, faces, OCR")
                componentToggle("dashboard", $dashboardOn, "Web dashboard",
                                dashboardStatus)
                if let componentError {
                    Label(componentError, systemImage: "exclamationmark.triangle.fill")
                        .font(.rowDetail)
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(Metrics.sm)
        } label: { Label("Components", systemImage: "square.stack.3d.up") }
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

        return VStack(alignment: .leading, spacing: Metrics.xs) {
            HStack(spacing: Metrics.md) {
                Toggle(isOn: action) { Text(title) }
                    .toggleStyle(.switch)
                    .disabled(applyingComponent != nil)
                // Turning the worker on runs a full start (extract, verify
                // sharp, preflight) and can take minutes. A row that just went
                // dead with no explanation reads as a hang.
                if applyingComponent == name {
                    ProgressView().controlSize(.small)
                    Text(binding.wrappedValue ? "Starting…" : "Stopping…")
                        .font(.rowDetail).foregroundStyle(.secondary)
                }
            }
            Text(caption).font(.rowDetail).foregroundStyle(.secondary)
        }
    }

    // Live state from the probe (not the toggle): reflects whether it actually
    // came up, and dodges an OrbStack port collision.
    private var dashboardStatus: String {
        if !model.snap.dashboardEnabled { return "Off" }
        return model.snap.dashboardUp
            ? "Running on localhost:\(model.snap.dashboardPort)" : "Starting…"
    }

    private var appVersion: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
    }

    private var updatesSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) {
                row("Menu bar app", "v\(appVersion)")
                row("Core", model.snap.version.isEmpty ? "-" : "v\(model.snap.version)")
                // The core auto-follows the app (see AppDelegate.syncCoreVersion);
                // this button drives the app's own Sparkle check.
                HStack {
                    Button("Check for Updates…") { UpdaterModel.shared.checkForUpdates() }
                    Spacer()
                }
            }
            .padding(4)
        } label: { Label("Software Update", systemImage: "arrow.down.circle") }
    }

    private var apiKey: String { config["api_key"] as? String ?? "" }

    private var keySection: some View {
        GroupBox {
            HStack(spacing: 8) {
                Text("API key").foregroundStyle(.secondary)
                    .frame(width: 90, alignment: .leading)
                // Distinguish a genuinely-missing key (breaks job counts and
                // authenticated calls) from a present-but-hidden one.
                Text(apiKey.isEmpty ? "not set"
                     : (revealKey ? apiKey : String(repeating: "•", count: 24)))
                    .font(.system(.callout, design: .monospaced))
                    .foregroundStyle(apiKey.isEmpty ? .secondary : .primary)
                    .textSelection(.enabled)
                Spacer()
                if !apiKey.isEmpty {
                    Button {
                        revealKey.toggle()
                    } label: { Image(systemName: revealKey ? "eye.slash" : "eye") }
                        .buttonStyle(.borderless)
                }
            }
            .padding(4)
        } label: { Label("Credentials", systemImage: "key.fill") }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 10) {
            Toggle(isOn: $launchAtLogin) { Text("Launch menu bar at login") }
                .toggleStyle(.switch)
                .onChange(of: launchAtLogin) { _, on in LaunchAtLogin.set(on) }
            HStack(spacing: 10) {
                Button("Reveal Config") { Actions.revealConfig() }
                Button("Open Logs") { Actions.openLogs() }
                Spacer()
                Text("v\(model.snap.version)").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - helpers

    private func str(_ key: String) -> String {
        if let s = config[key] as? String { return s }
        if let i = config[key] as? Int { return String(i) }
        return "-"
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack(spacing: 8) {
            Text(label).foregroundStyle(.secondary).frame(width: 90, alignment: .leading)
            Text(value).textSelection(.enabled)
            Spacer()
        }
        .font(.callout)
    }
}
