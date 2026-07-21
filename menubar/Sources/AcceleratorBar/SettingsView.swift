import SwiftUI

// A compact settings/info window: see how the accelerator is wired, switch the
// ML engine, and reach the config and logs. Read-mostly; the one mutation
// (engine switch) is explicit and warns that it restarts the service.
struct SettingsView: View {
    @ObservedObject var model: StatusModel
    @AppStorage("hasOnboarded") private var hasOnboarded = false

    @State private var config: [String: Any] = [:]
    @State private var engine = "native"
    @State private var savedEngine = "native"
    @State private var revealKey = false
    @State private var applying = false
    @State private var dashboardOn = true
    @State private var launchAtLogin = LaunchAtLogin.isEnabled

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            mlSection
            dashboardSection
            configSection
            keySection
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
        dashboardOn = (config["dashboard"] as? Bool) ?? true
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

    private var dashboardSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                Toggle(isOn: $dashboardOn) { Text("Web dashboard") }
                    .toggleStyle(.switch)
                    .onChange(of: dashboardOn) { _, on in
                        Task { await Actions.setDashboard(on); await model.refresh() }
                    }
                // Live state from the probe (not the toggle): reflects whether
                // it actually came up, and dodges an OrbStack port collision.
                row("Status", dashboardStatus)
            }
            .padding(4)
        } label: { Label("Dashboard", systemImage: "gauge.with.dots.needle.50percent") }
    }

    private var dashboardStatus: String {
        if !model.snap.dashboardEnabled { return "off" }
        return model.snap.dashboardUp
            ? "running on localhost:\(model.snap.dashboardPort)" : "starting…"
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
