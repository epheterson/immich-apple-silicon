import AppKit
import SwiftUI
import UniformTypeIdentifiers

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
        case general, processing, diagnostics

        // Identity is the case itself, so List's selection binding is a Pane?
        // rather than the String? an id-of-String would demand. Getting this
        // wrong still compiles, via a different List overload, and then simply
        // fails to track the selection.
        var id: Self { self }

        var title: String {
            switch self {
            case .general: return "General"
            case .processing: return "Processing"
            case .diagnostics: return "Diagnostics"
            }
        }
        var symbol: String {
            switch self {
            case .general: return "gearshape.fill"
            case .processing: return "cpu.fill"
            case .diagnostics: return "stethoscope"
            }
        }
        var tint: Color {
            switch self {
            case .general: return .gray
            case .processing: return .indigo
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
    @State private var hardwareVideoOn = true
    @State private var hardwareDecodeOn = true
    @State private var hardwareAudioOn = false
    @State private var applyingSwitch: String?
    @State private var encodingError: String?
    // Separate from encodingError: "saved, not yet live" is information, and
    // showing it in red alongside real failures teaches people to ignore both.
    @State private var encodingNotice: String?
    @State private var comparing = false
    @State private var compareResult: String?
    @State private var launchAtLogin = LaunchAtLogin.isEnabled
    @State private var mountSharesAtLogin = MountSharesAtLogin.isEnabled

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
        case .processing: processingTab
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
        loadSwitches()
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

                VStack(alignment: .leading, spacing: Metrics.xs) {
                    Toggle("Mount NAS shares at login", isOn: $mountSharesAtLogin)
                        .onChange(of: mountSharesAtLogin) { _, on in
                            Task { await MountSharesAtLogin.set(on) }
                        }
                    // Turning this on remembers whatever SMB shares (e.g. a
                    // NAS backing a split deployment) are mounted right now;
                    // it doesn't ask which ones separately.
                    // Naming this "at login" is accurate for what the switch
                    // does and misleading about the whole picture: the
                    // accelerator watches the library mount the entire time it
                    // runs and puts it back on its own. Someone reading only
                    // this row would reasonably conclude a share dropping at
                    // 2am is not handled until they log in again.
                    Text("Remembers the SMB shares mounted right now and reconnects any that are missing on launch. Separately, while the accelerator is running it watches the mount holding your library and remounts it on its own, retrying with a backoff.")
                        .font(.rowDetail).foregroundStyle(.secondary)
                }
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

    // MARK: - Processing

    /// Everything this Mac does to a photo or video, on one screen.
    ///
    /// The preset is the top control because it is the only decision most
    /// people need to make: how far from Docker's output this install is
    /// willing to move. The switches below are the same setting at a finer
    /// grain, shown rather than hidden so the preset is never a black box.
    private var processingTab: some View {
        Form {
            if workerOn {
            Section {
                // Centred, and no section header: the control is the first
                // thing in the pane and a word above it saying "Output" only
                // repeats what the descriptions underneath already say.
                HStack {
                    Spacer()
                    Picker("", selection: presetBinding) {
                        ForEach(Self.visiblePresets(current: currentPreset),
                                id: \.name) { preset in
                            Text(preset.title).tag(preset.name)
                        }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                    .disabled(applyingSwitch != nil)
                    Spacer()
                }

                // Laid out where the ends sit on the control: Stock on the
                // left, Apple Silicon on the right, so the description a
                // person reads is under the choice it belongs to.
                HStack(alignment: .top, spacing: Metrics.lg) {
                    ForEach(Self.visiblePresets(current: currentPreset),
                            id: \.name) { preset in
                        VStack(alignment: .leading, spacing: Metrics.xs) {
                            HStack(spacing: Metrics.md) {
                                Text(preset.title)
                                    .font(.rowDetail)
                                    .foregroundStyle(
                                        preset.name == currentPreset ? .primary : .secondary)
                                if let engine = preset.engine {
                                    BadgeLabel(text: engine,
                                               tint: engine == "NATIVE" ? .green : .orange)
                                }
                            }
                            Text(preset.detail)
                                .font(.rowDetail).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }

                Text("This covers transcoding. Machine learning is chosen separately below.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }

            }

            Section("Services") {
                componentToggle("worker", $workerOn, "Worker",
                                "Thumbnails, video, metadata")
                componentToggle("ml", $mlOn, "Machine Learning",
                                "Search, faces, text")
                componentToggle("dashboard", $dashboardOn, "Web Dashboard",
                                dashboardStatus)
            }

            if workerOn {
                Section("Hardware Transcoding") {
                    encodingToggle(
                        "hardware-decode", $hardwareDecodeOn, "Decoding",
                        "Video, thumbnails and previews")
                    encodingToggle(
                        "hardware-video", $hardwareVideoOn, "Video Encoding",
                        "H.264 and HEVC on VideoToolbox")
                    encodingToggle(
                        "hardware-audio", $hardwareAudioOn, "Audio Encoding",
                        "AAC on AudioToolbox")
                }
            }

            if mlOn {
                Section("Machine Learning Engine") {
                    Picker("Engine", selection: $engine) {
                        ForEach(["native", "python"], id: \.self) { value in
                            BadgeLabel(text: value == "native" ? "NATIVE" : "PYTHON",
                                       tint: value == "native" ? .green : .orange)
                                .tag(value)
                        }
                    }
                    if engine != savedEngine {
                        HStack(spacing: Metrics.md) {
                            Text("Restarts the accelerator.")
                                .font(.rowDetail).foregroundStyle(.secondary)
                            Spacer()
                            Button(applying ? "Applying…" : "Apply") {
                                applying = true
                                Task {
                                    let result = await Actions.setMLEngine(engine)
                                    if result.ok {
                                        savedEngine = engine
                                        encodingError = nil
                                        encodingNotice = result.message.isEmpty
                                            ? nil : result.message
                                    } else {
                                        // Was Void, so a failed write left the
                                        // picker claiming the new engine.
                                        encodingError = result.message
                                        engine = savedEngine
                                    }
                                    await model.refresh()
                                    applying = false
                                }
                            }
                            // Also blocked while a preset or switch write
                            // is in flight: setMLEngine rewrites the whole
                            // config in process, so overlapping with a CLI
                            // write loses whichever landed first.
                            .disabled(applying || applyingSwitch != nil
                                      || applyingComponent != nil)
                        }
                    }
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
                }
            }

            if workerOn {
                Section {
                    LabeledContent("Compare encoders") {
                        Button(comparing ? "Comparing…" : "Choose Video…") { compareEncoders() }
                            .disabled(comparing)
                    }
                    if let compareResult {
                        ScrollView {
                            Text(compareResult)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .frame(maxHeight: Metrics.compareResultHeight)
                    }
                } footer: {
                    Text("Runs both encoders on a file of yours and reports the difference.")
                        .font(.rowDetail).foregroundStyle(.secondary)
                }
            }

            if let message = encodingError ?? componentError {
                Section {
                    Label(message, systemImage: "exclamationmark.triangle.fill")
                        .font(.rowDetail)
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if let notice = encodingNotice, encodingError == nil, componentError == nil {
                Section {
                    Label(notice, systemImage: "info.circle")
                        .font(.rowDetail)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .formStyle(.grouped)
    }

    /// Two ends and the middle you land in by setting switches yourself.
    /// `engine` is the machine learning engine the end requires, shown as a
    /// pill so Stock moving you to the Python engine is never a surprise.
    /// Order is the order on the control: Stock on the left, Custom between
    /// them, Apple Silicon on the right. Custom is the middle of the range
    /// rather than a fourth option tacked on the end, and a switch moved off
    /// either end lands there.
    private static let presets: [(name: String, title: String, engine: String?, detail: String)] = [
        ("software", "Software", nil,
         "Transcoding done exactly as Immich's own container does it: software encoders, software decoding. Video and thumbnails are byte for byte what Docker produces. Uses the most CPU."),
        ("hardware", "Hardware", nil,
         "VideoToolbox for decoding, video and audio. Much less CPU, so the Mac keeps up with everything else it is doing. Video is visually identical to Docker's; audio and 10-bit thumbnails differ byte for byte."),
    ]

    /// Inserted between the two ends when the switches spell neither.
    private static let customPreset = (
        name: "custom", title: "Custom", engine: String?.none,
        detail: "Switches set individually, below.")

    /// The ends, with Custom in the middle only while it is the current state.
    private static func visiblePresets(current: String)
        -> [(name: String, title: String, engine: String?, detail: String)] {
        guard current == "custom" else { return presets }
        return [presets[0], customPreset, presets[1]]
    }

    private var presetEngine: String? {
        Self.visiblePresets(current: currentPreset)
            .first { $0.name == currentPreset }?.engine
    }

    private var currentPreset: String { StatusModel.encodingPreset(config) }

    /// Reads the derived preset, writes through the CLI. Selecting the position
    /// already active is ignored, so re-rendering cannot trigger a write.
    private var presetBinding: Binding<String> {
        Binding<String>(
            get: { currentPreset },
            set: { name in
                // Selecting Custom would have nothing to apply: it describes a
                // mixture rather than naming one, so it reports and never sets.
                guard name != currentPreset, name != "custom",
                      applyingSwitch == nil else { return }
                applyingSwitch = "preset"
                Task {
                    let result = await Actions.setEncodingPreset(name)
                    encodingError = result.ok ? nil : result.message
                    encodingNotice = result.ok && !result.message.isEmpty
                        ? result.message : nil
                    config = StatusModel.readConfig()
                    loadSwitches()
                    // Re-seed everything the write could have moved, not just
                    // the switches. Leaving the engine picker on its old value
                    // means an Apply there writes the stale engine back.
                    savedEngine = (config["ml_engine"] as? String) ?? "native"
                    engine = savedEngine
                    applyingSwitch = nil
                }
            })
    }

    private func loadSwitches() {
        hardwareVideoOn = StatusModel.encodingSwitchOn("IMMICH_ACCEL_HW_VIDEO", config)
        hardwareDecodeOn = StatusModel.encodingSwitchOn("IMMICH_ACCEL_HW_DECODE", config)
        hardwareAudioOn = StatusModel.encodingSwitchOn("IMMICH_ACCEL_HW_AUDIO", config)
    }

    // MARK: - Processing helpers

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

    private var dashboardStatus: String {
        if !model.snap.dashboardEnabled { return "Off" }
        return model.snap.dashboardUp
            ? "Running on localhost:\(model.snap.dashboardPort)" : "Starting…"
    }

    private func encodingToggle(
        _ name: String, _ binding: Binding<Bool>, _ title: String, _ caption: String
    ) -> some View {
        let action = Binding<Bool>(
            get: { binding.wrappedValue },
            set: { on in
                guard applyingSwitch == nil else { return }
                binding.wrappedValue = on
                applyingSwitch = name
                Task {
                    let result = await Actions.setEncodingSwitch(name, on)
                    if !result.ok {
                        encodingError = result.message
                        binding.wrappedValue = !on
                    } else {
                        encodingError = nil
                        encodingNotice = result.message.isEmpty ? nil : result.message
                        config = StatusModel.readConfig()
                    }
                    applyingSwitch = nil
                }
            })

        return Toggle(isOn: action) {
            VStack(alignment: .leading, spacing: Metrics.xs) {
                HStack(spacing: Metrics.md) {
                    Text(title)
                    if applyingSwitch == name {
                        ProgressView().controlSize(.small)
                    }
                }
                Text(caption).font(.rowDetail).foregroundStyle(.secondary)
            }
        }
        .toggleStyle(.switch)
        .disabled(applyingSwitch != nil)
    }

    private func compareEncoders() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.movie, .video, .quickTimeMovie, .mpeg4Movie]
        panel.allowsMultipleSelection = false
        panel.prompt = "Compare"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        comparing = true
        compareResult = nil
        Task {
            compareResult = await Actions.encodeCompare(url.path)
            comparing = false
        }
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
