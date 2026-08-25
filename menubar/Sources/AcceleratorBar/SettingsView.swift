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
        case general, services, transcoding, library, diagnostics

        // Identity is the case itself, so List's selection binding is a Pane?
        // rather than the String? an id-of-String would demand. Getting this
        // wrong still compiles, via a different List overload, and then simply
        // fails to track the selection.
        var id: Self { self }

        var title: String {
            switch self {
            case .general: return "General"
            case .services: return "Services"
            case .transcoding: return "Transcoding"
            case .library: return "Library"
            case .diagnostics: return "Diagnostics"
            }
        }
        var symbol: String {
            switch self {
            case .general: return "gearshape.fill"
            case .services: return "square.stack.3d.up.fill"
            case .transcoding: return "film.fill"
            case .library: return "photo.on.rectangle.angled"
            case .diagnostics: return "stethoscope"
            }
        }
        var tint: Color {
            switch self {
            case .general: return .gray
            case .services: return .indigo
            case .transcoding: return .purple
            case .library: return .orange
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
    @State private var hardwareDecodeOn = true
    @State private var hardwareVideoOn = true
    @State private var hardwareAudioOn = false
    @State private var applyingSwitch: String?
    @State private var encodingError: String?
    // Saved-but-not-live. A separate channel from encodingError on
    // purpose: see the spec. Showing it in red beside real failures
    // teaches people to ignore both.
    @State private var notice: String?
    @State private var comparing = false
    @State private var compareResult: String?
    @State private var launchAtLogin = LaunchAtLogin.isEnabled
    // Homebrew refusing to load the formula, which reads identically to
    // "you are up to date" everywhere else in the app.
    @State private var updatesBlocked = false
    @State private var trusting = false
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
        case .services: servicesTab
        case .transcoding: transcodingTab
        case .library: libraryTab
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
        hardwareDecodeOn = StatusModel.encodingSwitchOn("IMMICH_ACCEL_HW_DECODE", config)
        hardwareVideoOn = StatusModel.encodingSwitchOn("IMMICH_ACCEL_HW_VIDEO", config)
        hardwareAudioOn = StatusModel.encodingSwitchOn("IMMICH_ACCEL_HW_AUDIO", config)
        // Off the main thread: this shells out to brew, which on a cold cache
        // takes seconds, and the window must not wait on it to draw.
        Task { updatesBlocked = await Actions.brewRefusesTap() }
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
                    Toggle("Reconnect SMB shares at launch", isOn: $mountSharesAtLogin)
                        .onChange(of: mountSharesAtLogin) { _, on in
                            Task { await MountSharesAtLogin.set(on) }
                        }
                    // Turning this on remembers whatever SMB shares (e.g. a
                    // NAS backing a split deployment) are mounted right now;
                    // it doesn't ask which ones separately.
                    // Says which shares, and says it only covers launch, because
                    // the accelerator remounts the library on its own the whole
                    // time it runs and the two were easy to confuse.
                    Text("Remembers every SMB share mounted when you switch this on, this Mac's own as well as Immich's, and reconnects any that have gone missing the next time this app launches. Your Immich library does not need this: the accelerator records how it is mounted and puts it back on its own, the whole time it runs.")
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
                if updatesBlocked {
                    VStack(alignment: .leading, spacing: Metrics.xs) {
                        Label("Updates are blocked", systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                        // What is happening and what it costs, before the fix.
                        // "Untrusted tap" alone reads like a warning about us.
                        Text("Homebrew will not load the formula until you trust the tap, so "
                             + "brew upgrade leaves this Mac on the version it already has, "
                             + "and says nothing.")
                            .font(.rowDetail).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        HStack(spacing: Metrics.md) {
                            Button(trusting ? "Trusting…" : "Trust the Tap") {
                                trusting = true
                                Task {
                                    // A refused write looked identical to a
                                    // successful one that did not take: the
                                    // button flipped back and the same warning
                                    // sat there. brew's own text says why.
                                    let result = await Actions.trustTap()
                                    updatesBlocked = await Actions.brewRefusesTap()
                                    trusting = false
                                    if !result.ok || updatesBlocked {
                                        record((ok: false,
                                                message: result.output.isEmpty
                                                    ? "brew trust did not take effect."
                                                    : result.output))
                                    }
                                }
                            }
                            .disabled(trusting)
                            // Shown as well as offered: this changes the
                            // user's Homebrew configuration, so anyone who
                            // would rather run it themselves can read it and
                            // copy it.
                            Text("brew trust \(Actions.tap)")
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                    }
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

    // MARK: - Services

    /// What runs on this Mac, and which engine does the machine learning.
    ///
    /// Split from Transcoding because they answer different questions: this
    /// one is "is it on", that one is "how does it encode". They were one
    /// screen and it ran long enough that the controls at the bottom were
    /// below the fold on an unmodified window.
    private var servicesTab: some View {
        Form {
            Section("Services") {
                componentToggle("worker", $workerOn, "Worker",
                                "Thumbnails, video, metadata")
                componentToggle("ml", $mlOn, "Machine Learning",
                                "Search, faces, text")
                componentToggle("dashboard", $dashboardOn, "Web Dashboard",
                                dashboardStatus)
            }

            if mlOn {
                engineSection
            }

            messageSections
        }
        .formStyle(.grouped)
    }

    // MARK: - Transcoding

    /// How this Mac encodes video, and the tool for deciding.
    ///
    /// Every control here writes switches only the worker reads, so with no
    /// worker there is nothing to configure and the pane says so rather than
    /// showing controls over nothing.
    private var transcodingTab: some View {
        Form {
            if workerOn {
                positionSection

                Section("Hardware Transcoding") {
                    encodingToggle("hardware-decode", $hardwareDecodeOn,
                                   "Decoding", "Video, thumbnails and previews")
                    encodingToggle("hardware-video", $hardwareVideoOn,
                                   "Video Encoding", "H.264 and HEVC on VideoToolbox")
                    encodingToggle("hardware-audio", $hardwareAudioOn,
                                   "Audio Encoding", "AAC on AudioToolbox")
                }

                compareSection
            } else {
                Section {
                    Text("The worker is switched off, so nothing on this Mac is "
                         + "transcoding. Turn it on under Services.")
                        .font(.rowDetail).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            messageSections
        }
        .formStyle(.grouped)
    }

    /// Failures in red, then saved-but-not-live in secondary text.
    ///
    /// Two channels on purpose: showing "saved, takes effect later" in red
    /// beside real errors teaches people to ignore both. Shared by every pane
    /// that can write, so a message cannot appear on one and not the other.
    @ViewBuilder
    private var messageSections: some View {
        if let message = encodingError ?? componentError {
            Section {
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .font(.rowDetail)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        if let note = notice ?? encodingNotice {
            Section {
                Label(note, systemImage: "info.circle")
                    .font(.rowDetail)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - Library

    /// Where the photos are, how they are reached, and whether that is working.
    ///
    /// This exists because the mount story was split across a caption in
    /// General and nothing at all: the SMB toggle there is launch-only and
    /// covers every share on the Mac, while the library is watched and
    /// remounted continuously by the accelerator and may not even be SMB. On
    /// the release Mac it is NFS, so that toggle never touched it.
    private var libraryTab: some View {
        Form {
            Section("Library") {
                LabeledContent("Location", value: libraryPath)
                if let recipe = libraryMount {
                    LabeledContent("Mounted", value: recipe)
                }
                LabeledContent("State") {
                    HStack(spacing: Metrics.md) {
                        Text(libraryState.label)
                        StatusDot(state: libraryState.dot)
                    }
                }
            } footer: {
                Text("The accelerator checks this every 30 seconds while it "
                     + "runs. If the mount goes away it pauses the worker and "
                     + "puts the mount back, retrying at 1, 5 and 15 minutes, "
                     + "and stops for good if the server rejects the "
                     + "credentials rather than locking the account out.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }

            Section("Other Shares") {
                Toggle("Reconnect SMB shares at launch", isOn: $mountSharesAtLogin)
                    .onChange(of: mountSharesAtLogin) { _, on in
                        Task { await MountSharesAtLogin.set(on) }
                    }
            } footer: {
                Text("Remembers every SMB share mounted when you switch this "
                     + "on, and reconnects any that have gone missing the next "
                     + "time this app launches. Separate from the library "
                     + "above, which needs no help.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    private var positionSection: some View {
        Section {
            HStack {
                Spacer()
                Picker("", selection: positionBinding) {
                    // Custom appears only while it is the state. It reports a
                    // mixture rather than naming one, so there is nothing to
                    // apply and it must not look like something you can pick.
                    // Marking the segment disabled does not work: the macOS
                    // segmented style renders every label identically no
                    // matter what, measured at the same pixel value as its
                    // neighbours, so the only honest way to say "not a choice"
                    // is to leave it out until it is the answer. Same rule the
                    // descriptions below already follow.
                    ForEach(Self.visiblePositions(current: currentPosition), id: \.name) { position in
                        Text(position.title)
                            .tag(position.name)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                .disabled(writeInFlight)
                Spacer()
            }

            HStack(alignment: .top, spacing: Metrics.lg) {
                ForEach(Self.visiblePositions(current: currentPosition), id: \.name) { position in
                    VStack(alignment: .leading, spacing: Metrics.xs) {
                        Text(position.title)
                            .font(.rowDetail)
                            .foregroundStyle(position.name == currentPosition ? .primary : .secondary)
                        Text(position.detail)
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

    private var engineSection: some View {
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
                    Button(applying ? "Applying…" : "Apply") { applyEngine() }
                        // One guard for the whole pane: setMLEngine rewrites
                        // the config in process while everything else goes
                        // through the CLI, so an overlap loses a write.
                        .disabled(writeInFlight)
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

    private var compareSection: some View {
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
            Text("Runs every encoder on a file of yours and reports the difference.")
                .font(.rowDetail).foregroundStyle(.secondary)
        }
    }

    // MARK: - Processing state

    /// The two named positions and the derived middle, in control order.
    static let positions: [(name: String, title: String, detail: String)] = [
        ("software", "Software",
         "The encoders and decoders Immich's own container uses. Video and thumbnails come out byte for byte what Docker produces, except for a file ffmpeg cannot decode at all, whose thumbnail comes from QuickLook. Uses the most CPU."),
        ("custom", "Custom", "Some on, some off. Set below."),
        ("hardware", "Hardware",
         "VideoToolbox for decoding, video and audio. Much less CPU, so the Mac keeps up with everything else it is doing. Video is visually identical to Docker's; audio and 10-bit thumbnails differ byte for byte."),
    ]

    /// The positions to show, in control order. The control and the
    /// descriptions under it read from this one function so they cannot drift
    /// into showing different sets, which is what they did when each filtered
    /// for itself.
    static func visiblePositions(current: String)
        -> [(name: String, title: String, detail: String)]
    {
        positions.filter { $0.name != "custom" || current == "custom" }
    }

    private var currentPosition: String { StatusModel.encodingPreset(config) }

    /// True while any write is in flight, anywhere on the pane.
    private var writeInFlight: Bool {
        applying || applyingSwitch != nil || applyingComponent != nil
    }

    /// Reads the derived position, writes through the CLI. Selecting the
    /// position already active does nothing, so a redraw cannot cause a write.
    private var positionBinding: Binding<String> {
        Binding<String>(
            get: { currentPosition },
            set: { name in
                guard name != currentPosition, name != "custom", !writeInFlight else { return }
                applyingSwitch = "position"
                Task {
                    record(await Actions.setEncodingPreset(name))
                    reload()
                    applyingSwitch = nil
                }
            })
    }

    private func applyEngine() {
        applying = true
        Task {
            let result = await Actions.setMLEngine(engine)
            if result.ok {
                savedEngine = engine
                record(result)
            } else {
                // Put the picker back: it must not claim an engine that was
                // never written.
                encodingError = result.message
                engine = savedEngine
            }
            await model.refresh()
            applying = false
        }
    }

    /// Route one result into the two channels the spec defines.

    /// A spinner that occupies its space whether or not it is spinning.
    ///
    /// Inserting one shifts every row below it, which lands at the exact
    /// moment someone is reaching for the next control. A settings window
    /// must not move under the pointer.
    private func spinnerSlot(_ active: Bool) -> some View {
        ProgressView()
            .controlSize(.small)
            .opacity(active ? 1 : 0)
            .frame(width: 16, height: 16)
    }

    private func record(_ result: (ok: Bool, message: String)) {
        encodingError = result.ok ? nil : result.message
        notice = result.ok && !result.message.isEmpty ? result.message : nil
    }

    /// Re-read everything a write could have moved. Leaving the engine picker
    /// stale means an Apply there writes the old value back.
    private func reload() {
        config = StatusModel.readConfig()
        workerOn = StatusModel.componentEnabled("worker", config)
        mlOn = StatusModel.componentEnabled("ml", config)
        dashboardOn = StatusModel.componentEnabled("dashboard", config)
        hardwareDecodeOn = StatusModel.encodingSwitchOn("IMMICH_ACCEL_HW_DECODE", config)
        hardwareVideoOn = StatusModel.encodingSwitchOn("IMMICH_ACCEL_HW_VIDEO", config)
        hardwareAudioOn = StatusModel.encodingSwitchOn("IMMICH_ACCEL_HW_AUDIO", config)
        savedEngine = (config["ml_engine"] as? String) ?? "native"
        engine = savedEngine
    }

    /// One encoding switch. Optimistic, and put back if the CLI refuses, so a
    /// switch never claims something the accelerator did not do.
    private func encodingToggle(
        _ name: String, _ binding: Binding<Bool>, _ title: String, _ caption: String
    ) -> some View {
        let action = Binding<Bool>(
            get: { binding.wrappedValue },
            set: { on in
                guard !writeInFlight else { return }
                binding.wrappedValue = on
                applyingSwitch = name
                Task {
                    let result = await Actions.setEncodingSwitch(name, on)
                    if !result.ok { binding.wrappedValue = !on }
                    record(result)
                    if result.ok { reload() }
                    applyingSwitch = nil
                }
            })

        return Toggle(isOn: action) {
            VStack(alignment: .leading, spacing: Metrics.xs) {
                HStack(spacing: Metrics.md) {
                    Text(title)
                    spinnerSlot(applyingSwitch == name)
                }
                Text(caption).font(.rowDetail).foregroundStyle(.secondary)
            }
        }
        .toggleStyle(.switch)
        .disabled(writeInFlight)
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
                    spinnerSlot(applyingComponent == name)
                }
                // Turning the worker on runs a full start (extract, verify
                // sharp, preflight) and can take minutes, so a row that just
                // went dead with no explanation reads as a hang. The progress
                // word replaces the caption rather than joining it: the line
                // is already there, so swapping its text moves nothing.
                Text(applyingComponent == name
                     ? (binding.wrappedValue ? "Starting…" : "Stopping…")
                     : caption)
                    .font(.rowDetail).foregroundStyle(.secondary)
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

    // MARK: - Library facts

    private var libraryPath: String {
        (config["upload_mount"] as? String) ?? "not configured"
    }

    /// "nfs from 10.0.0.14:/volume1/ELP NAS", when the accelerator has
    /// recorded how the mount is put together. It records it while the mount
    /// is up, because it cannot be read back once the mount is gone.
    private var libraryMount: String? {
        guard let recipe = config["mount_recipe"] as? [String: Any],
              let fstype = recipe["fstype"] as? String,
              let spec = recipe["spec"] as? String
        else { return nil }
        return "\(fstype) from \(spec)"
    }

    /// Reachable, or not, and whether that has already stopped the worker.
    /// FileManager, not a mount table parse: the question is whether the
    /// directory the worker reads is actually there.
    private var libraryState: (label: String, dot: StatusModel.State) {
        let path = libraryPath
        guard path != "not configured" else { return ("Not configured", .off) }
        if let paused = model.snap.pausedReason, paused.contains("library") {
            return ("Missing, worker paused", .bad)
        }
        return FileManager.default.fileExists(atPath: path)
            ? ("Reachable", .good)
            : ("Missing", .bad)
    }

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
