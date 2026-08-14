import AppKit
import SwiftUI

/// First-run setup, done in the app.
///
/// What this replaces: a screen that said "setup runs in Terminal", opened
/// Terminal, and then declared "You're all set" the moment `config.json`
/// existed. That was wrong in three ways. It never asked what the Mac was
/// for, so `setup --ml-only` (a spare Mac serving ML for an Immich running
/// elsewhere, no Docker, no database, no library access) was unreachable
/// unless you read the README. It handed the user to a terminal and lost every
/// signal about what happened there. And a config file existing does not mean
/// the worker started, the engine answers, or the API key is any good, so
/// "all set" was a guess.
///
/// So: ask what the Mac is for, collect only what the CLI cannot guess, run
/// setup here with its output on screen, then prove it works.
@MainActor
final class WizardModel: ObservableObject {
    enum Step: Int, CaseIterable {
        case role, where_, newImmich, connect, library, install, run, verify
    }

    /// Everything a split deployment needs, so the app can complete one itself.
    /// The wizard collects these rather than sending anyone to Terminal: a GUI
    /// that hands off has failed at the one job it exists for.
    struct RemoteDetails {
        var dbHost = ""
        var dbPort = "5432"
        var dbUser = "postgres"
        var dbPassword = ""
        var dbName = "immich"
        var redisHost = ""
        var redisPort = "6379"
        var redisUser = ""
        var redisPassword = ""
        var mediaPath = ""

        /// Redis usually lives beside Postgres; don't make people type it twice.
        var effectiveRedisHost: String { redisHost.isEmpty ? dbHost : redisHost }
        var ready: Bool { !dbHost.isEmpty && !dbPassword.isEmpty }
    }

    /// Where Immich itself runs. This is the question the wizard never asked,
    /// and getting it wrong is not cosmetic: `cmd_setup` dispatches on whether
    /// a URL was supplied, so always sending one routed every user to the
    /// split-deployment path and made the local Docker flow — the primary
    /// documented topology, the one that reads DB credentials straight out of
    /// the container — unreachable from the app.
    enum Location {
        case here      // Docker on this Mac; the CLI detects everything
        case remote    // another machine; needs URL + API key
    }

    /// Two independent switches, not a choice between two packages.
    ///
    /// This was a pair of mutually exclusive role cards, "Everything" and
    /// "Machine learning only", which made a simple thing sound like a
    /// commitment and had no way to express "worker but let Immich keep doing
    /// ML". These map one-to-one onto the components the CLI and Settings
    /// already have, so what you tick here is what you see there afterwards.
    struct Components {
        var microservices = true
        var machineLearning = true

        /// No worker means an ML-only node, which is a genuinely different
        /// setup: no Docker, no database, no access to the photos.
        var isMLOnly: Bool { !microservices && machineLearning }
        var none: Bool { !microservices && !machineLearning }
    }

    @Published var step: Step = .role
    @Published var components = Components()
    /// nil until we know, either because detection has not run or because it
    /// could not answer (an older CLI with no `detect`, Docker unreachable).
    /// Deliberately not defaulted: guessing wrong here is what routed everyone
    /// down the remote path, so an unknown answer asks rather than assumes.
    @Published var location: Location?
    @Published var detecting = false
    /// Whatever `immich-accelerator detect` last reported. nil = not asked yet.
    @Published var detected: Detection?
    /// Docker is here but Immich is not, so setup would be creating one.
    var wantsNewImmich: Bool {
        guard let d = detected else { return false }
        return d.askedSuccessfully && !d.foundLocalImmich
    }
    @Published var photosPath = ""
    @Published var dataPath = ""
    @Published var remote = RemoteDetails()

    // Library reachability, checked rather than assumed. nil = not checked yet.
    @Published var libraryReadable: Bool?
    @Published var libraryChecking = false
    @Published var libraryNote = ""

    /// True while the services started by setup are still coming up.
    @Published var settling = false
    @Published var libraryCandidates: [String] = []
    @Published var dbReachable: Bool?
    @Published var redisReachable: Bool?
    @Published var probingPorts = false
    @Published var url = ""
    @Published var apiKey = ""

    /// nil = not checked yet. Set by probing, never by guessing from the text.
    @Published var urlReachable: Bool?
    @Published var keyValid: Bool?
    @Published var probing = false
    @Published var immichVersion = ""

    @Published var log: [String] = []
    @Published var working = false
    @Published var failed = false

    private let model: StatusModel

    /// True when this Mac is already set up, which changes what the wizard is
    /// for: not a first run but an edit, so it starts from what is there.
    let isRerun = Paths.isConfigured

    init(model: StatusModel) {
        self.model = model
        prefillFromExistingConfig()
        // Lets `render wizard:<step>` capture any screen, not just the first.
        // Every step after the first is otherwise unreachable headlessly, which
        // is how a dead-ended Install step and an unasked topology question
        // both shipped unlooked-at. Dev/CI only; unset in every real launch.
        if let want = ProcessInfo.processInfo.environment["ACCEL_WIZARD_STEP"],
           let target = Step.allCases.first(where: { "\($0)" == want || "\($0)" == want + "_" }) {
            step = target
            // Both of these only exist on the remote path, so a render that
            // asks for one has to put the model in the state that shows it.
            if target == .connect || target == .library { location = .remote }
        }
    }

    /// The steps this run will actually visit. ML-only skips Connect entirely,
    /// because an ML node is not a client of anything: another Immich points at
    /// it, so it needs no URL and no key.
    var steps: [Step] {
        var out: [Step] = [.role]
        if components.microservices { out += [.where_, .newImmich, .connect, .library] }
        if needsInstall { out.append(.install) }
        out += [.run, .verify]
        return out
    }

    /// Captured once, not re-read from the filesystem on every access.
    ///
    /// `steps` used to ask `Paths.isInstalled` each time it was evaluated, so
    /// installing the formula removed `.install` from the array while the user
    /// was standing on it. `advance()` then looked for the current step in a
    /// list that no longer contained it, found nothing, and returned: the
    /// wizard dead-ended on its own success, and the only way out was the skip
    /// button, which closed it without ever running setup.
    @Published private(set) var needsInstall = !Paths.isInstalled

    struct Detection {
        /// True when the CLI answered at all. False means we do not know, which
        /// is different from knowing there is nothing here.
        var askedSuccessfully = false
        var dockerFound = false
        var immichVersion: String?
        var mediaLocation: String?
        var note: String?          // why nothing was found, in the CLI's words
        var foundLocalImmich: Bool { immichVersion != nil }
    }

    /// Connect is only meaningful for a server on another machine. A local
    /// Docker Immich needs no URL and no key from the user: the CLI reads both
    /// out of the container.
    var visibleSteps: [Step] {
        steps.filter { step in
            // Only when Immich is meant to be here and there is none yet.
            if step == .newImmich {
                return location == .some(.here) && wantsNewImmich
            }
            // Both only exist for a server on another machine: a local Docker
            // Immich needs no credentials typed and shares this Mac's paths.
            guard step == .connect || step == .library else { return true }
            return location == .some(.remote)
        }
    }

    var canGoBack: Bool { step != .role && !working }

    // Navigation never silently does nothing. Both used to `guard let i =
    // firstIndex(of: step) else { return }`, so the moment the current step
    // fell off the visible path — which happens whenever a choice above it
    // changes what comes next — the buttons became inert with no explanation.
    // That is exactly how the install step trapped people, and it would have
    // recurred for every conditional step added later. Falling back to
    // declaration order means the worst case is landing on an unexpected step,
    // not being stuck on a dead one.
    func advance() {
        let path = visibleSteps
        guard let i = path.firstIndex(of: step) else {
            step = path.first { $0.rawValue > step.rawValue } ?? path.last ?? step
            return
        }
        if i + 1 < path.count { step = path[i + 1] }
    }

    func goBack() {
        let path = visibleSteps
        guard let i = path.firstIndex(of: step) else {
            step = path.last { $0.rawValue < step.rawValue } ?? path.first ?? step
            return
        }
        if i > 0 { step = path[i - 1] }
    }

    /// Start from what setup last wrote, so a re-run shows the current answers
    /// rather than an empty form. We tell people to re-run after an update;
    /// an empty form there reads as "your settings are gone".
    private func prefillFromExistingConfig() {
        guard let cfg = Actions.existingConfig() else { return }
        func str(_ key: String) -> String {
            if let v = cfg[key] as? String { return v }
            if let v = cfg[key] as? Int { return String(v) }
            return ""
        }
        url = str("immich_url")
        apiKey = str("api_key")
        remote.dbHost = str("db_hostname")
        remote.dbPort = str("db_port").isEmpty ? "5432" : str("db_port")
        remote.dbUser = str("db_username").isEmpty ? "postgres" : str("db_username")
        remote.dbName = str("db_name").isEmpty ? "immich" : str("db_name")
        remote.redisHost = str("redis_hostname")
        remote.redisPort = str("redis_port").isEmpty ? "6379" : str("redis_port")
        remote.redisUser = str("redis_username")
        remote.mediaPath = str("upload_mount")
        // The password is never read back: config.json holds it, but showing it
        // in a field would put it on screen and in a screenshot for no gain.
        // Left blank, and setup keeps the stored one if nothing is typed.

        // Components as they actually stand, so the toggles show today's truth.
        components.microservices = (cfg["worker"] as? Bool) ?? !((cfg["ml_only"] as? Bool) ?? false)
        components.machineLearning = (cfg["ml"] as? Bool) ?? true

        // A configured Mac with a URL is by definition talking to something
        // elsewhere; without one it is the local Docker case.
        location = url.isEmpty ? .here : .remote
    }

    /// Give the services a moment to come up before judging them.
    ///
    /// Bounded, and it stops early the moment things are running, so the
    /// verify step shows "Starting…" for as long as that is true and the real
    /// checks after that. Never blocks anything: the step is usable throughout.
    private func waitForServices(timeout: TimeInterval = 90) async {
        settling = true
        defer { settling = false }
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            await model.refresh()
            if model.snap.overall == .running { return }
            try? await Task.sleep(nanoseconds: 3_000_000_000)
        }
    }

    // MARK: - probing

    /// Ask Immich who it is, and (separately) whether the key is accepted.
    ///
    /// Two questions, two answers: a reachable server with a rejected key is a
    /// completely different fix from an unreachable one, and lumping them into
    /// one "connection failed" is what sends people to the wrong place.
    /// Immich, Postgres and Redis are on the same box in most split setups,
    /// so seed the host from the address already typed rather than asking for
    /// the same string three times.
    func seedRemoteHosts() {
        guard let host = URL(string: normalizedURL)?.host, !host.isEmpty else { return }
        if remote.dbHost.isEmpty { remote.dbHost = host }
        // Shown in the field rather than only implied, so what will be used is
        // what is on screen.
        if remote.redisHost.isEmpty { remote.redisHost = host }
    }

    func probe() async {
        let base = normalizedURL
        guard !base.isEmpty else { return }
        probing = true
        defer { probing = false }
        urlReachable = nil
        keyValid = nil
        immichVersion = ""

        guard let versionURL = URL(string: base + "/api/server/version") else {
            urlReachable = false
            return
        }
        var req = URLRequest(url: versionURL)
        req.timeoutInterval = 8
        if let (data, resp) = try? await URLSession.shared.data(for: req),
           (resp as? HTTPURLResponse)?.statusCode == 200 {
            urlReachable = true
            if let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let major = j["major"] as? Int,
               let minor = j["minor"] as? Int,
               let patch = j["patch"] as? Int {
                immichVersion = "\(major).\(minor).\(patch)"
            }
        } else {
            urlReachable = false
            return
        }

        guard !apiKey.isEmpty, let usersURL = URL(string: base + "/api/users/me") else { return }
        var keyReq = URLRequest(url: usersURL)
        keyReq.timeoutInterval = 8
        keyReq.setValue(apiKey, forHTTPHeaderField: "x-api-key")
        if let (_, resp) = try? await URLSession.shared.data(for: keyReq) {
            keyValid = (resp as? HTTPURLResponse)?.statusCode == 200
        } else {
            keyValid = false
        }
    }

    /// Accept what people actually paste: a bare host, a trailing slash, a
    /// copied URL with the port but no scheme.
    var normalizedURL: String {
        var s = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return "" }
        if !s.contains("://") { s = "http://" + s }
        while s.hasSuffix("/") { s.removeLast() }
        return s
    }

    var connectReady: Bool { urlReachable == true }

    // MARK: - doing the work

    func installAccelerator() async {
        working = true
        failed = false
        log = []
        let ok = await Actions.installFormula { line in
            Task { @MainActor in self.log.append(line) }
        }
        working = false
        failed = !ok
        if ok {
            // Advance BEFORE clearing needsInstall: advance() walks
            // visibleSteps, and dropping .install first would remove the step
            // we are standing on, which is exactly the dead-end this had.
            advance()
            needsInstall = false
        }
    }

    /// Ask the CLI what it can see. Never fails the flow: not finding Immich is
    /// an answer (that is the remote case, and the ML-only case), not an error.
    func detect() async {
        detecting = true
        defer { detecting = false }
        let d = await Actions.detect()
        detected = d
        // Only preselect. The user can always override, because a Mac can have
        // a local Immich and still be pointed at a different one.
        // Preselect only on evidence. "Found Immich here" and "asked and there
        // is none" are both answers; "could not ask" is not, and it must not
        // silently become "remote".
        if d.foundLocalImmich {
            location = .here
        } else if d.askedSuccessfully {
            location = .remote
        }
    }

    /// Can this Mac actually read the library, at the path Immich uses?
    ///
    /// A split deployment fails here more than anywhere else: the share is not
    /// mounted, or it is mounted somewhere else, and every job then fails one
    /// by one with nothing saying why. Checking is cheap and the answer is
    /// unambiguous, so check rather than hope, and make it retryable because
    /// the usual fix (mount the share) happens outside this app.
    /// Fill in everything that can be worked out rather than asked.
    ///
    /// Aim: for a common setup, arrive at this step with the path already
    /// found and green, and nothing to do but press Continue.
    func autofillLibrary() async {
        guard remote.mediaPath.isEmpty else { return await checkLibrary() }
        libraryChecking = true
        let found = await Actions.discoverLibraries()
        libraryCandidates = found
        libraryChecking = false
        if let only = found.first, found.count == 1 {
            remote.mediaPath = only
            await checkLibrary()
        } else if found.isEmpty {
            libraryNote = "No library found."
            libraryReadable = false
        } else {
            libraryNote = "\(found.count) libraries found. Choose one."
            libraryReadable = false
        }
    }

    /// Postgres and Redis reachability, checked in the form rather than
    /// discovered halfway through setup.
    func probePorts() async {
        probingPorts = true
        defer { probingPorts = false }
        async let db = Actions.probePort(host: remote.dbHost, port: remote.dbPort)
        async let redis = Actions.probePort(
            host: remote.effectiveRedisHost, port: remote.redisPort)
        dbReachable = await db
        redisReachable = await redis
    }

    func checkLibrary() async {
        libraryChecking = true
        defer { libraryChecking = false }
        let path = remote.mediaPath
        guard !path.isEmpty else {
            libraryReadable = false
            libraryNote = "Choose Immich's media folder."
            return
        }
        let result = await Actions.probeLibrary(path)
        libraryReadable = result.ok
        libraryNote = result.note
    }

    /// Ask the system to connect to a file server. macOS puts up its own
    /// authentication sheet and stores what it needs in the keychain, so no
    /// password is ever typed into, or held by, this app.
    func connectToServer() {
        Actions.openFileServerConnect(hint: remote.dbHost)
    }

    func runSetup() async {
        working = true
        failed = false
        log = []
        // The URL is what dispatches cmd_setup. Sending one for a local
        // Docker Immich would take the split-deployment path and ask the CLI
        // to hand-configure what it can read out of the container.
        let sendURL = (components.microservices && location == .some(.remote)) ? normalizedURL : ""
        let ok = await Actions.runSetup(
            url: sendURL, apiKey: sendURL.isEmpty ? "" : apiKey, mlOnly: components.isMLOnly,
            remote: sendURL.isEmpty ? nil : remote,
            newImmich: wantsNewImmich ? (photosPath, dataPath) : nil
        ) { line in
            Task { @MainActor in self.log.append(line) }
        }
        await model.refresh()
        working = false
        failed = !ok
        if ok {
            advance()
            Task { await waitForServices() }
        }
    }
}

struct SetupWizard: View {
    @ObservedObject var model: StatusModel
    @StateObject private var wiz: WizardModel
    @AppStorage("hasOnboarded") private var hasOnboarded = false

    init(model: StatusModel) {
        self.model = model
        _wiz = StateObject(wrappedValue: WizardModel(model: model))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            InsetDivider()
            ScrollView {
                content
                    .padding(Metrics.xxl)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            InsetDivider()
            footer
        }
        .frame(width: Metrics.wizardWidth, height: Metrics.wizardHeight)
        .background(WindowTitle(title: "Set Up Immich Accelerator"))
    }

    private var header: some View {
        HStack(spacing: Metrics.lg) {
            Image(systemName: "bolt.fill")
                .font(.title)
                .foregroundStyle(.yellow)
            VStack(alignment: .leading, spacing: Metrics.xs) {
                Text("Immich Accelerator").font(.headline)
                Text(stepBlurb).font(.rowDetail).foregroundStyle(.secondary)
            }
            Spacer()
            StepDots(steps: wiz.visibleSteps, current: wiz.step)
        }
        .padding(.horizontal, Metrics.xxl)
        .padding(.vertical, Metrics.xl)
    }

    private var stepBlurb: String {
        switch wiz.step {
        case .role: return "What should this Mac do?"
        case .where_: return "Where is Immich?"
        case .newImmich: return "Create Immich"
        case .library: return "Reaching your library"
        case .connect: return "Connect to Immich"
        case .install: return "Install the accelerator"
        case .run: return "Setting things up"
        case .verify: return "Checking it works"
        }
    }

    @ViewBuilder
    private var content: some View {
        switch wiz.step {
        case .role: roleStep
        case .where_: whereStep
        case .newImmich: newImmichStep
        case .library: libraryStep
        case .connect: connectStep
        case .install: installStep
        case .run: runStep
        case .verify: verifyStep
        }
    }

    // MARK: - steps

    /// The question the wizard never asked. Immich on this Mac in Docker is the
    /// primary documented topology and the one the CLI handles best: it reads
    /// the database and Redis credentials straight out of the running
    /// container. Sending a URL instead routes setup down the split-deployment
    /// path and asks the user to type all of that by hand.
    /// Docker is here, Immich is not. Setup can build the whole stack, and it
    /// needs exactly two answers to do it. They are asked here rather than
    /// defaulted, because a wrong guess creates a real Immich pointed at the
    /// wrong folder and re-running setup does not undo that.
    private var newImmichStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            Text("No Immich found. Setup can create one.")
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: Metrics.sm) {
                Text("Your photos").font(.rowTitle)
                HStack(spacing: Metrics.sm) {
                    TextField("~/Pictures", text: $wiz.photosPath)
                        .textFieldStyle(.roundedBorder)
                    Button("Choose…") { pick(into: { wiz.photosPath = $0 },
                                              message: "Choose your photo library.") }
                }
                Text("Mounted read only, for Immich to import from.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: Metrics.sm) {
                Text("Immich's data").font(.rowTitle)
                HStack(spacing: Metrics.sm) {
                    TextField("~/.immich-accelerator/data", text: $wiz.dataPath)
                        .textFieldStyle(.roundedBorder)
                    Button("Choose…") { pick(into: { wiz.dataPath = $0 },
                                              message: "Choose where Immich should store its data.") }
                }
                Text("Thumbnails, transcoded video and backups. This grows with your library.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }
        }
    }

    private func pick(into set: @escaping (String) -> Void, message: String) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use This Folder"
        panel.message = message
        if panel.runModal() == .OK, let url = panel.url { set(url.path) }
    }

    private var whereStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            if wiz.detecting {
                HStack(spacing: Metrics.sm) {
                    ProgressView().controlSize(.small)
                    Text("Looking for Immich…").foregroundStyle(.secondary)
                }
            }

            LocationCard(
                title: "On this Mac",
                blurb: wiz.detected?.foundLocalImmich == true
                    ? "Immich \(wiz.detected?.immichVersion ?? "") is running here. Nothing to enter."
                    : "Immich runs in Docker on this Mac.",
                symbol: "desktopcomputer",
                selected: wiz.location == .some(.here),
                recommended: wiz.detected?.foundLocalImmich == true
            ) {
                wiz.location = .here
                // Only now. Shelling out to Docker before you have said Immich
                // is here is work on behalf of someone who may not run Docker
                // at all, and it is slow when the daemon is down.
                Task { await wiz.detect() }
            }

            LocationCard(
                title: "On another machine",
                blurb: "Immich runs on a NAS or another server.",
                symbol: "network",
                selected: wiz.location == .some(.remote),
                recommended: false
            ) { wiz.location = .remote }

            if let note = wiz.detected?.note, wiz.detected?.foundLocalImmich != true {
                FailureNote(text: note)
            }
        }

    }

    /// The step that decides whether a split deployment actually works.
    ///
    /// Immich's worker resolves every media path from one root, and this Mac
    /// must see the same files at the same absolute path. When it does not,
    /// nothing announces it: jobs just fail, one at a time, forever. So the
    /// path is chosen explicitly, checked for real, and re-checkable, because
    /// mounting the share happens outside this window.
    private var libraryStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            Text("Immich's media folder, as this Mac sees it.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            // The single most common way a split deployment fails, and it fails
            // silently: Immich stores absolute paths, so the worker looks for
            // the exact string the server uses. A share mounted somewhere else
            // reads fine here and produces jobs that fail one at a time later.
            // Always, not only when we happen to know Immich's path. On a
            // split deployment we do not know it, and that is exactly the case
            // this catches.
            CalloutRow(
                text: pathHint,
                action: "How to map it",
                url: "https://github.com/epheterson/immich-apple-silicon#split-deployment-nas--mac")

            HStack(spacing: Metrics.sm) {
                TextField("/Volumes/photos/immich", text: $wiz.remote.mediaPath)
                    .textFieldStyle(.roundedBorder)
                Button("Choose…") { chooseLibraryFolder() }
            }

            HStack(spacing: Metrics.sm) {
                Button(wiz.libraryChecking ? "Checking…" : "Check again") {
                    Task { await wiz.checkLibrary() }
                }
                .disabled(wiz.libraryChecking)
                Button("Connect to Server…") { wiz.connectToServer() }
                    .help("Mount a network share.")
                if wiz.libraryChecking { ProgressView().controlSize(.small) }
            }

            if wiz.libraryCandidates.count > 1 {
                VStack(alignment: .leading, spacing: Metrics.sm) {
                    Text("Found on this Mac").font(.rowTitle)
                    ForEach(wiz.libraryCandidates, id: \.self) { candidate in
                        Button(candidate) {
                            wiz.remote.mediaPath = candidate
                            Task { await wiz.checkLibrary() }
                        }
                        .buttonStyle(.link)
                    }
                }
            }

            if let ok = wiz.libraryReadable {
                ProbeRow(ok: ok, good: wiz.libraryNote, bad: wiz.libraryNote)
            }
        }
        .task { if wiz.libraryReadable == nil { await wiz.autofillLibrary() } }
    }

    /// Immich stores absolute paths, so the worker looks for the exact string
    /// the server uses. A share mounted somewhere else reads fine here and
    /// produces jobs that fail one at a time later, with nothing pointing at
    /// the cause.
    private var pathHint: String {
        if let want = wiz.detected?.mediaLocation, !want.isEmpty {
            return "Immich uses \(want). This Mac has to reach the same files at that exact path."
        }
        return "This path has to match the one Immich itself uses, exactly. If they differ, setup succeeds and jobs fail afterwards."
    }

    private func chooseLibraryFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use This Folder"
        panel.message = "Choose Immich's media folder."
        if panel.runModal() == .OK, let picked = panel.url {
            wiz.remote.mediaPath = picked.path
            Task { await wiz.checkLibrary() }
        }
    }

    private var roleStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            ComponentToggle(
                title: "Microservices",
                blurb: "Thumbnails, video transcoding, metadata.",
                symbol: "square.stack.3d.up.fill",
                on: $wiz.components.microservices)

            ComponentToggle(
                title: "Machine learning",
                blurb: "Search, faces, text recognition.",
                symbol: "brain.head.profile",
                on: $wiz.components.machineLearning)

            if wiz.components.isMLOnly {
                Text("No Docker, database, or access to your photos. Point another Immich at this Mac.")
                    .font(.rowDetail).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if wiz.components.none {
                Text("Turn on at least one.")
                    .font(.rowDetail).foregroundStyle(.orange)
            }
        }
    }

    private var connectStep: some View {
        VStack(alignment: .leading, spacing: Metrics.xl) {
            VStack(alignment: .leading, spacing: Metrics.md) {
                Text("Immich address").font(.rowTitle)
                TextField("nas.local:2283", text: $wiz.url)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await wiz.probe() } }
            }

            VStack(alignment: .leading, spacing: Metrics.md) {
                Text("API key").font(.rowTitle)
                SecureField("paste your key", text: $wiz.apiKey)
                    .textFieldStyle(.roundedBorder)
                Text("Immich → Account Settings → API Keys. Optional.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }

            HStack(spacing: Metrics.md) {
                Button(wiz.probing ? "Checking…" : "Check connection") {
                    Task { await wiz.probe(); wiz.seedRemoteHosts() }
                }
                .disabled(wiz.probing || wiz.normalizedURL.isEmpty)
                if wiz.probing { ProgressView().controlSize(.small) }
                Spacer()
            }

            probeResults

            Divider()

            // The database and Redis details setup would otherwise ask for in
            // a terminal. Collected here so the app can finish the job itself.
            // A worker talks to Postgres and Redis directly; the API alone is
            // not enough, which is why a split deployment needs these at all.
            VStack(alignment: .leading, spacing: Metrics.md) {
                Text("Database and Redis").font(.rowTitle)

                HStack(spacing: Metrics.sm) {
                    TextField("Postgres host", text: $wiz.remote.dbHost)
                        .textFieldStyle(.roundedBorder)
                    TextField("Port", text: $wiz.remote.dbPort)
                        .textFieldStyle(.roundedBorder).frame(width: 70)
                }
                HStack(spacing: Metrics.sm) {
                    TextField("User", text: $wiz.remote.dbUser)
                        .textFieldStyle(.roundedBorder).frame(width: 130)
                    SecureField("Password", text: $wiz.remote.dbPassword)
                        .textFieldStyle(.roundedBorder)
                    TextField("Database", text: $wiz.remote.dbName)
                        .textFieldStyle(.roundedBorder).frame(width: 120)
                }
                HStack(spacing: Metrics.sm) {
                    TextField("Redis host (same as Postgres if blank)", text: $wiz.remote.redisHost)
                        .textFieldStyle(.roundedBorder)
                    TextField("Port", text: $wiz.remote.redisPort)
                        .textFieldStyle(.roundedBorder).frame(width: 70)
                }
                HStack(spacing: Metrics.md) {
                    Button(wiz.probingPorts ? "Checking…" : "Check database") {
                        Task { await wiz.probePorts() }
                    }
                    .disabled(wiz.probingPorts || wiz.remote.dbHost.isEmpty)
                    if wiz.probingPorts { ProgressView().controlSize(.small) }
                }
                if let db = wiz.dbReachable {
                    ProbeRow(ok: db,
                             good: "Postgres reachable.",
                             bad: "No response from \(wiz.remote.dbHost):\(wiz.remote.dbPort).")
                }
                if let r = wiz.redisReachable {
                    ProbeRow(ok: r,
                             good: "Redis reachable.",
                             bad: "No response from \(wiz.remote.effectiveRedisHost):\(wiz.remote.redisPort).")
                }
            }
        }
    }

    @ViewBuilder
    private var probeResults: some View {
        VStack(alignment: .leading, spacing: Metrics.md) {
            if let reachable = wiz.urlReachable {
                ProbeRow(
                    ok: reachable,
                    good: wiz.immichVersion.isEmpty
                        ? "Immich answered." : "Immich \(wiz.immichVersion).",
                    bad: "No response from \(wiz.normalizedURL).")
            }
            // Only meaningful once the server answered: a key cannot be
            // judged against a server that never replied.
            if wiz.urlReachable == true, let valid = wiz.keyValid {
                ProbeRow(
                    ok: valid,
                    good: "API key accepted.",
                    bad: "Key rejected.")
            }
        }
    }

    private var installStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            Text("Accelerator not installed.").font(.rowTitle)
            Text("Installs with Homebrew. Takes a few minutes.")
                .foregroundStyle(.secondary)
            if wiz.working || !wiz.log.isEmpty { LogPane(lines: wiz.log, working: wiz.working) }
            if wiz.failed {
                FailureNote(text: "Install failed.\n\(Actions.installCommand)")
            }
        }
    }

    private var runStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            Text(wiz.components.isMLOnly
                 ? "Setting up machine learning."
                 : "Setting up.").font(.rowTitle)
            if wiz.components.microservices {
                Text("Downloads Immich's server files. Takes a few minutes.")
                    .foregroundStyle(.secondary)
            }
            // Reopened from Settings on a box that already works. Setup is the
            // documented repair path and re-running it is safe, but it does
            // rewrite config.json, so say that before the button is pressed
            // rather than after.
            if Paths.isConfigured && wiz.log.isEmpty && !wiz.working {
                FailureNote(text: "Already set up. Running setup again rewrites the configuration.")
            }
            if wiz.working || !wiz.log.isEmpty { LogPane(lines: wiz.log, working: wiz.working) }
            if wiz.failed {
                FailureNote(text: "Setup failed. See the output above.")
            }
        }
    }

    private var verifyStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            // Services take a while to come up, and setup hands over the moment
            // it returns. Reporting "Needs attention" for that window is both
            // alarming and wrong: nothing needs attention, it is starting.
            if wiz.settling {
                HStack(spacing: Metrics.sm) {
                    ProgressView().controlSize(.small)
                    Text("Starting…").foregroundStyle(.secondary)
                }
            }
            // Real state, not "config.json exists". Each row is the live
            // snapshot the menu bar uses.
            ForEach(Diagnostics.checks(config: StatusModel.readConfig(), snap: model.snap)) { c in
                HStack(alignment: .firstTextBaseline, spacing: Metrics.md) {
                    Image(systemName: c.iconName)
                        .symbolRenderingMode(.hierarchical)
                        .foregroundStyle(c.tint)
                        .frame(width: Metrics.iconColumn)
                    Text(c.label).frame(width: 110, alignment: .leading)
                    Text(c.detail).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .font(.callout)
            }
            HStack(spacing: Metrics.md) {
                Button("Re-check") { Task { await model.refresh() } }
                if let t = model.lastMLTest {
                    Text(t).font(.rowDetail).foregroundStyle(.secondary)
                } else {
                    Button("Run ML self-test") {
                        Task { model.lastMLTest = await Actions.mlTest() }
                    }
                    .disabled(!model.snap.mlHealthy)
                }
                Spacer()
            }
            .padding(.top, Metrics.sm)
        }
    }

    // MARK: - footer

    private var footer: some View {
        HStack(spacing: Metrics.md) {
            if wiz.canGoBack {
                Button("Back") { wiz.goBack() }
            }
            Spacer()
            if wiz.step != .verify {
                // "Skip for now" only makes sense the first time, when there
                // is something to postpone. Re-running from Settings on a
                // working Mac, the same button means "leave things as they
                // are", and calling that skipping implies you failed to do
                // something.
                Button(wiz.isRerun ? "Cancel" : "Skip for now") { close() }
                    .buttonStyle(.link)
            }
            primaryButton
        }
        .padding(.horizontal, Metrics.xxl)
        .padding(.vertical, Metrics.xl)
    }

    @ViewBuilder
    private var primaryButton: some View {
        switch wiz.step {
        case .role:
            Button("Continue") { wiz.advance() }
                .keyboardShortcut(.defaultAction)
        case .connect:
            Button(wiz.connectReady ? "Continue" : "Continue anyway") { wiz.advance() }
                .keyboardShortcut(.defaultAction)
                .disabled(wiz.normalizedURL.isEmpty)
        case .where_:
            Button("Continue") { wiz.advance() }
                .keyboardShortcut(.defaultAction)
                .disabled(wiz.detecting || wiz.location == nil)
        case .newImmich:
            Button("Continue") { wiz.advance() }
                .keyboardShortcut(.defaultAction)
                .disabled(wiz.photosPath.isEmpty || wiz.dataPath.isEmpty)
        case .library:
            Button("Continue") { wiz.advance() }
                .keyboardShortcut(.defaultAction)
                .disabled(wiz.libraryReadable != true)
        case .install:
            Button(wiz.working ? "Installing…" : (wiz.failed ? "Try again" : "Install")) {
                Task { await wiz.installAccelerator() }
            }
            .keyboardShortcut(.defaultAction)
            .disabled(wiz.working)
        case .run:
            Button(wiz.working ? "Setting up…" : (wiz.failed ? "Try again" : "Run setup")) {
                Task { await wiz.runSetup() }
            }
            .keyboardShortcut(.defaultAction)
            .disabled(wiz.working)
        case .verify:
            Button("Done") { hasOnboarded = true; close() }
                .keyboardShortcut(.defaultAction)
        }
    }

    private func close() {
        hasOnboarded = true
        NSApp.keyWindow?.performClose(nil)
    }
}

// MARK: - pieces

private struct LocationCard: View {
    let title: String
    let blurb: String
    let symbol: String
    let selected: Bool
    let recommended: Bool
    let pick: () -> Void

    var body: some View {
        Button(action: pick) {
            HStack(alignment: .top, spacing: Metrics.md) {
                Image(systemName: symbol)
                    .font(.system(size: 22))
                    .frame(width: 34)
                    .foregroundStyle(selected ? Color.accentColor : .secondary)
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: Metrics.sm) {
                        Text(title).font(.headline)
                        if recommended {
                            Text("Detected")
                                .font(.caption2.weight(.semibold))
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .background(Color.accentColor.opacity(0.15), in: Capsule())
                                .foregroundStyle(Color.accentColor)
                        }
                    }
                    Text(blurb)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(Metrics.md)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(selected ? Color.accentColor.opacity(0.10) : Color.secondary.opacity(0.06))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(selected ? Color.accentColor : Color.secondary.opacity(0.25),
                                  lineWidth: selected ? 2 : 1)
            )
        }
        .buttonStyle(.plain)
    }
}

private struct CalloutRow: View {
    let text: String
    let action: String
    let url: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Metrics.md) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 4) {
                Text(text)
                    .font(.rowDetail)
                    .fixedSize(horizontal: false, vertical: true)
                Link(action, destination: URL(string: url)!)
                    .font(.rowDetail)
            }
            Spacer(minLength: 0)
        }
        .padding(Metrics.md)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
    }
}

private struct ComponentToggle: View {
    let title: String
    let blurb: String
    let symbol: String
    @Binding var on: Bool

    var body: some View {
        HStack(alignment: .top, spacing: Metrics.md) {
            Image(systemName: symbol)
                .font(.system(size: 22))
                .frame(width: 34)
                .foregroundStyle(on ? Color.accentColor : .secondary)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(blurb)
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: Metrics.md)
            Toggle("", isOn: $on)
                .labelsHidden()
                .toggleStyle(.switch)
                .accessibilityLabel(title)
        }
        .padding(Metrics.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10).fill(Color.secondary.opacity(0.06)))
    }
}

private struct StepDots: View {
    let steps: [WizardModel.Step]
    let current: WizardModel.Step

    var body: some View {
        HStack(spacing: Metrics.sm) {
            ForEach(steps, id: \.rawValue) { s in
                Circle()
                    .fill(s == current ? Color.accentColor : Color.secondary.opacity(0.3))
                    .frame(width: 6, height: 6)
            }
        }
    }
}

/// Setup's own output, live. Auto-scrolled, because the interesting line is
/// always the last one.
private struct LogPane: View {
    let lines: [String]
    let working: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.sm) {
            HStack {
                Spacer()
                Button("Copy") {
                    Actions.copyToPasteboard(lines.joined(separator: "\n"))
                }
                .buttonStyle(.link)
                .disabled(lines.isEmpty)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    // One Text, not one per line. Per-line Text views each own
                    // their own selection, so a drag could never cross a line
                    // boundary and the output could not be copied out whole,
                    // which is the only reason to look at it after a failure.
                    Text(lines.joined(separator: "\n"))
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(Metrics.md)
                        .id(lines.count)
                }
                .frame(height: 180)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Color(nsColor: .textBackgroundColor))
                )
                .onChange(of: lines.count) { _, n in
                    withAnimation { proxy.scrollTo(n, anchor: .bottom) }
                }
            }
            if working {
                HStack(spacing: Metrics.md) {
                    ProgressView().controlSize(.small)
                    Text("Working…")
                        .font(.rowDetail).foregroundStyle(.secondary)
                }
            }
        }
    }
}

private struct ProbeRow: View {
    let ok: Bool
    let good: String
    let bad: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Metrics.md) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(ok ? .green : .orange)
            Text(ok ? good : bad)
                .font(.rowDetail)
                .foregroundStyle(ok ? .secondary : .primary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }
}

private struct FailureNote: View {
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Metrics.md) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            Text(text)
                .font(.rowDetail)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
    }
}
