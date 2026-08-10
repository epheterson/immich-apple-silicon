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
        case role, where_, connect, install, run, verify
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

    enum Role: String {
        case everything, mlOnly

        var title: String {
            switch self {
            case .everything: return "Everything"
            case .mlOnly: return "Machine learning only"
            }
        }
        var blurb: String {
            switch self {
            case .everything:
                return "This Mac processes your library: thumbnails, video transcoding, metadata, search, faces and text."
            case .mlOnly:
                return "This Mac only answers search, face and text requests for an Immich running somewhere else. No Docker, no database, no access to your photos."
            }
        }
        var symbol: String {
            switch self {
            case .everything: return "square.stack.3d.up.fill"
            case .mlOnly: return "brain.head.profile"
            }
        }
    }

    @Published var step: Step = .role
    @Published var role: Role = .everything
    /// nil until we know, either because detection has not run or because it
    /// could not answer (an older CLI with no `detect`, Docker unreachable).
    /// Deliberately not defaulted: guessing wrong here is what routed everyone
    /// down the remote path, so an unknown answer asks rather than assumes.
    @Published var location: Location?
    @Published var detecting = false
    /// Whatever `immich-accelerator detect` last reported. nil = not asked yet.
    @Published var detected: Detection?
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

    init(model: StatusModel) {
        self.model = model
        // Lets `render wizard:<step>` capture any screen, not just the first.
        // Every step after the first is otherwise unreachable headlessly, which
        // is how a dead-ended Install step and an unasked topology question
        // both shipped unlooked-at. Dev/CI only; unset in every real launch.
        if let want = ProcessInfo.processInfo.environment["ACCEL_WIZARD_STEP"],
           let target = Step.allCases.first(where: { "\($0)" == want || "\($0)" == want + "_" }) {
            step = target
            if target == .connect { location = .remote }   // that step only exists for remote
        }
    }

    /// The steps this run will actually visit. ML-only skips Connect entirely,
    /// because an ML node is not a client of anything: another Immich points at
    /// it, so it needs no URL and no key.
    var steps: [Step] {
        var out: [Step] = [.role]
        if role == .everything { out += [.where_, .connect] }
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
        steps.filter { $0 != .connect || location != .here }
    }

    var canGoBack: Bool { step != .role && !working }

    func advance() {
        guard let i = visibleSteps.firstIndex(of: step), i + 1 < visibleSteps.count else { return }
        step = visibleSteps[i + 1]
    }

    func goBack() {
        guard let i = visibleSteps.firstIndex(of: step), i > 0 else { return }
        step = visibleSteps[i - 1]
    }

    // MARK: - probing

    /// Ask Immich who it is, and (separately) whether the key is accepted.
    ///
    /// Two questions, two answers: a reachable server with a rejected key is a
    /// completely different fix from an unreachable one, and lumping them into
    /// one "connection failed" is what sends people to the wrong place.
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

    func runSetup() async {
        working = true
        failed = false
        log = []
        // The URL is what dispatches cmd_setup. Sending one for a local
        // Docker Immich would take the split-deployment path and ask the CLI
        // to hand-configure what it can read out of the container.
        let sendURL = (role == .everything && location == .some(.remote)) ? normalizedURL : ""
        let ok = await Actions.runSetup(
            url: sendURL, apiKey: sendURL.isEmpty ? "" : apiKey, mlOnly: role == .mlOnly
        ) { line in
            Task { @MainActor in self.log.append(line) }
        }
        await model.refresh()
        working = false
        failed = !ok
        if ok { advance() }
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
    private var whereStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            if wiz.detecting {
                HStack(spacing: Metrics.sm) {
                    ProgressView().controlSize(.small)
                    Text("Looking for Immich on this Mac…").foregroundStyle(.secondary)
                }
            }

            LocationCard(
                title: "On this Mac",
                blurb: wiz.detected?.foundLocalImmich == true
                    ? "Found Immich \(wiz.detected?.immichVersion ?? "") running in Docker. Setup will read its settings from the container, so there is nothing to type."
                    : "Immich runs in Docker here. Setup will find it and read its settings from the container.",
                symbol: "desktopcomputer",
                selected: wiz.location == .some(.here),
                recommended: wiz.detected?.foundLocalImmich == true
            ) { wiz.location = .here }

            LocationCard(
                title: "On another machine",
                blurb: "A NAS or another server runs Immich. This Mac needs its address and an API key, and needs to reach the same library files.",
                symbol: "network",
                selected: wiz.location == .some(.remote),
                recommended: false
            ) { wiz.location = .remote }

            if let note = wiz.detected?.note, wiz.detected?.foundLocalImmich != true {
                FailureNote(text: note)
            }
        }
        .task { if wiz.detected == nil { await wiz.detect() } }
    }

    private var roleStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            ForEach([WizardModel.Role.everything, .mlOnly], id: \.rawValue) { r in
                RoleCard(role: r, selected: wiz.role == r) { wiz.role = r }
            }
            Text("You can change any of this later in Settings, without running setup again.")
                .font(.rowDetail).foregroundStyle(.secondary)
                .padding(.top, Metrics.sm)
        }
    }

    private var connectStep: some View {
        VStack(alignment: .leading, spacing: Metrics.xl) {
            VStack(alignment: .leading, spacing: Metrics.md) {
                Text("Immich address").font(.rowTitle)
                TextField("nas.local:2283", text: $wiz.url)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await wiz.probe() } }
                Text("The address you open Immich at. A bare host and port is fine.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: Metrics.md) {
                Text("API key").font(.rowTitle)
                SecureField("paste your key", text: $wiz.apiKey)
                    .textFieldStyle(.roundedBorder)
                Text("Immich → Account Settings → API Keys. Used for job counts and the Re-queue button. You can add it later.")
                    .font(.rowDetail).foregroundStyle(.secondary)
            }

            HStack(spacing: Metrics.md) {
                Button(wiz.probing ? "Checking…" : "Check connection") {
                    Task { await wiz.probe() }
                }
                .disabled(wiz.probing || wiz.normalizedURL.isEmpty)
                if wiz.probing { ProgressView().controlSize(.small) }
                Spacer()
            }

            probeResults
        }
    }

    @ViewBuilder
    private var probeResults: some View {
        VStack(alignment: .leading, spacing: Metrics.md) {
            if let reachable = wiz.urlReachable {
                ProbeRow(
                    ok: reachable,
                    good: wiz.immichVersion.isEmpty
                        ? "Immich answered." : "Immich \(wiz.immichVersion) answered.",
                    bad: "Nothing answered at \(wiz.normalizedURL). Check the address and that Immich is running.")
            }
            // Only meaningful once the server answered: a key cannot be
            // judged against a server that never replied.
            if wiz.urlReachable == true, let valid = wiz.keyValid {
                ProbeRow(
                    ok: valid,
                    good: "API key accepted.",
                    bad: "Immich rejected that key. Setup will still finish; job counts stay off until it's fixed.")
            }
        }
    }

    private var installStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            Text("The accelerator command isn't installed yet.").font(.rowTitle)
            Text("It installs with Homebrew, which also keeps it updated. This takes a few minutes the first time, mostly downloading.")
                .foregroundStyle(.secondary)
            if wiz.working || !wiz.log.isEmpty { LogPane(lines: wiz.log, working: wiz.working) }
            if wiz.failed {
                FailureNote(text: "The install did not finish. The output above says why; you can also run it yourself:\n\(Actions.installCommand)")
            }
        }
    }

    private var runStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
            Text(wiz.role == .mlOnly
                 ? "Setting this Mac up as an ML node."
                 : "Connecting to Immich and preparing the worker.").font(.rowTitle)
            if wiz.role == .everything {
                Text("Setup downloads the matching Immich server files and checks Docker, the database and your media paths. Several minutes is normal.")
                    .foregroundStyle(.secondary)
            }
            // Reopened from Settings on a box that already works. Setup is the
            // documented repair path and re-running it is safe, but it does
            // rewrite config.json, so say that before the button is pressed
            // rather than after.
            if Paths.isConfigured && wiz.log.isEmpty && !wiz.working {
                FailureNote(text: "This Mac is already set up. Running setup again re-detects everything and rewrites the configuration. Your API key, ML address and component switches are carried across. Back up first from Settings → General if you want a copy.")
            }
            if wiz.working || !wiz.log.isEmpty { LogPane(lines: wiz.log, working: wiz.working) }
            if wiz.failed {
                FailureNote(text: "Setup stopped early. The output above says why. Nothing is broken; you can fix the cause and run this step again.")
            }
        }
    }

    private var verifyStep: some View {
        VStack(alignment: .leading, spacing: Metrics.lg) {
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
                Button("Skip for now") { close() }
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

private struct RoleCard: View {
    let role: WizardModel.Role
    let selected: Bool
    let pick: () -> Void

    var body: some View {
        Button(action: pick) {
            HStack(alignment: .top, spacing: Metrics.lg) {
                Image(systemName: role.symbol)
                    .font(.title2)
                    .foregroundStyle(selected ? Color.accentColor : .secondary)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: Metrics.sm) {
                    Text(role.title).font(.rowTitle)
                    Text(role.blurb)
                        .font(.rowDetail)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(selected ? Color.accentColor : .secondary.opacity(0.4))
            }
            .padding(Metrics.lg)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color.primary.opacity(selected ? 0.07 : 0.03))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(selected ? Color.accentColor : .clear, lineWidth: 2)
            )
        }
        .buttonStyle(.plain)
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
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { i, line in
                            Text(line)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(i)
                        }
                    }
                    .padding(Metrics.md)
                }
                .frame(height: 180)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Color(nsColor: .textBackgroundColor))
                )
                .onChange(of: lines.count) { _, n in
                    withAnimation { proxy.scrollTo(n - 1, anchor: .bottom) }
                }
            }
            if working {
                HStack(spacing: Metrics.md) {
                    ProgressView().controlSize(.small)
                    Text("Working. You can leave this window open.")
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
