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
    // One flag for all three: applying a change shells out to the CLI, and the
    // toggles should not race each other into a contradictory config.
    @State private var applyingComponent = false
    @State private var componentError: String?
    @State private var launchAtLogin = LaunchAtLogin.isEnabled

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            mlSection
            componentsSection
            configSection
            keySection
            updatesSection
            Divider()
            footer
        }
        .padding(20)
        .frame(width: 460)
        .onAppear(perform: load)
    }

    private func load() {
        config = StatusModel.readConfig()
        savedEngine = (config["ml_engine"] as? String) ?? "native"
        engine = savedEngine
        // Seed the switches without treating it as a user action: assigning
        // here fires .onChange, so an install that already has a component off
        // would shell out to turn it off merely because the window opened.
        applyingComponent = true
        workerOn = StatusModel.componentEnabled("worker", config)
        mlOn = StatusModel.componentEnabled("ml", config)
        dashboardOn = StatusModel.componentEnabled("dashboard", config)
        applyingComponent = false
    }

    // MARK: - sections

    private var configSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Diagnostics.checks(config: config, snap: model.snap)) { c in
                    HStack(spacing: 8) {
                        Image(systemName: c.iconName).foregroundStyle(c.tint).frame(width: 16)
                        Text(c.label).foregroundStyle(.secondary)
                            .frame(width: 90, alignment: .leading)
                        Text(c.detail).textSelection(.enabled)
                            .foregroundStyle(c.level == .fail ? .primary : .secondary)
                        Spacer()
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
            .padding(4)
        } label: { Label("Configuration", systemImage: "checklist") }
    }

    private var mlSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
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
            .padding(4)
        } label: { Label("Machine Learning", systemImage: "brain.fill") }
    }

    // The accelerator's three separable processes. This is as fine-grained as
    // it gets: video, thumbnails and RAW decode all run inside the one worker,
    // so which of those happen is Immich's job scheduler, not ours.
    private var componentsSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                componentToggle("worker", $workerOn, "Worker",
                                "Thumbnails, video transcoding, metadata")
                componentToggle("ml", $mlOn, "Machine Learning",
                                "Search, faces, OCR")
                componentToggle("dashboard", $dashboardOn, "Web dashboard",
                                dashboardStatus)
                if let componentError {
                    Text(componentError).font(.caption).foregroundStyle(.red)
                }
            }
            .padding(4)
        } label: { Label("Components", systemImage: "square.stack.3d.up") }
    }

    private func componentToggle(
        _ name: String, _ binding: Binding<Bool>, _ title: String, _ caption: String
    ) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Toggle(isOn: binding) { Text(title) }
                .toggleStyle(.switch)
                .disabled(applyingComponent)
                .onChange(of: binding.wrappedValue) { _, on in
                    // Ignore the assignment load() makes when seeding state.
                    guard !applyingComponent else { return }
                    applyingComponent = true
                    Task {
                        let result = await Actions.setComponent(name, on)
                        await model.refresh()
                        // Never leave a switch claiming something the
                        // accelerator did not do: put it back and say why.
                        if !result.ok {
                            componentError = result.message
                            applyingComponent = true
                            binding.wrappedValue = !on
                        } else {
                            componentError = nil
                            config = StatusModel.readConfig()
                        }
                        applyingComponent = false
                    }
                }
            Text(caption).font(.caption).foregroundStyle(.secondary)
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
