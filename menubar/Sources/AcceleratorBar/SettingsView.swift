import ServiceManagement
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
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            immichSection
            mlSection
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
    }

    // MARK: - sections

    private var immichSection: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) {
                row("Immich", model.snap.immichVersion.isEmpty ? "—" : model.snap.immichVersion)
                row("Connects to", str("immich_url"))
                row("Public domain", model.snap.externalDomain.isEmpty
                    ? "not set (Open Immich uses the local address)" : model.snap.externalDomain)
            }
            .padding(4)
        } label: { Label("Immich", systemImage: "photo.on.rectangle.angled") }
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

    private var keySection: some View {
        GroupBox {
            HStack(spacing: 8) {
                Text("API key").foregroundStyle(.secondary)
                    .frame(width: 90, alignment: .leading)
                Text(revealKey ? str("api_key") : String(repeating: "•", count: 24))
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                Spacer()
                Button {
                    revealKey.toggle()
                } label: { Image(systemName: revealKey ? "eye.slash" : "eye") }
                    .buttonStyle(.borderless)
            }
            .padding(4)
        } label: { Label("Credentials", systemImage: "key.fill") }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 10) {
            Toggle(isOn: $launchAtLogin) { Text("Launch menu bar at login") }
                .toggleStyle(.switch)
                .onChange(of: launchAtLogin) { _, on in
                    try? on ? SMAppService.mainApp.register() : SMAppService.mainApp.unregister()
                }
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
        return "—"
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
