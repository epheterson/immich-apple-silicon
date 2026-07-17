import SwiftUI

// First-run guidance. Meets the user wherever they are: not installed, installed
// but not set up, or ready. The heavy lifting (the interactive setup) stays in
// the CLI; this just points the way and confirms when everything is live.
struct OnboardingView: View {
    @ObservedObject var model: StatusModel
    @AppStorage("hasOnboarded") private var hasOnboarded = false
    @State private var recheck = 0
    @State private var copied = false

    private enum Phase { case notInstalled, notConfigured, ready }

    private var phase: Phase {
        _ = recheck  // re-evaluate the filesystem checks when Recheck is tapped
        if !Paths.isInstalled { return .notInstalled }
        if !Paths.isConfigured { return .notConfigured }
        return .ready
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            header
            Divider()
            switch phase {
            case .notInstalled: notInstalled
            case .notConfigured: notConfigured
            case .ready: ready
            }
        }
        .padding(22)
        .frame(width: 460)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "bolt.fill")
                .font(.system(size: 30))
                .foregroundStyle(.yellow)
            VStack(alignment: .leading, spacing: 2) {
                Text("Immich Accelerator").font(.title2).bold()
                Text("Run Immich's ML and video work natively on Apple Silicon")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - phases

    private var notInstalled: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("The accelerator isn't installed yet", systemImage: "shippingbox")
                .font(.headline)
            Text("Install it with Homebrew, then come back and re-check.")
                .foregroundStyle(.secondary)
            HStack(spacing: 8) {
                Text(Actions.installCommand)
                    .font(.system(.footnote, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
                Button {
                    Actions.copyToPasteboard(Actions.installCommand)
                    copied = true
                } label: {
                    Image(systemName: copied ? "checkmark" : "doc.on.doc")
                }
                .help("Copy")
            }
            recheckRow
        }
    }

    private var notConfigured: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Installed — let's connect it to Immich", systemImage: "gearshape")
                .font(.headline)
            Text("Setup asks a few questions (your Immich, database, media) and "
                 + "starts the services. It runs in Terminal.")
                .foregroundStyle(.secondary)
            Button {
                Actions.runSetupInTerminal()
            } label: {
                Label("Run Setup in Terminal", systemImage: "terminal")
                    .frame(maxWidth: .infinity)
            }
            .controlSize(.large)
            .buttonStyle(.borderedProminent)
            recheckRow
        }
    }

    private var ready: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("You're all set", systemImage: "checkmark.seal.fill")
                .font(.headline)
                .foregroundStyle(.green)
            VStack(alignment: .leading, spacing: 4) {
                if !model.snap.immichVersion.isEmpty {
                    infoLine("Immich", model.snap.immichVersion)
                }
                if !model.snap.openImmichURL.isEmpty {
                    infoLine("Address", model.snap.openImmichURL)
                }
                infoLine("Status", model.snap.overall.label)
            }
            .font(.callout)
            HStack {
                Button("Open Immich") { Actions.openImmich(model.snap.openImmichURL) }
                Spacer()
                Button("Done") {
                    hasOnboarded = true
                    NSApp.keyWindow?.performClose(nil)
                }
                .keyboardShortcut(.defaultAction)
            }
        }
    }

    // MARK: - bits

    private var recheckRow: some View {
        HStack {
            Spacer()
            Button {
                recheck += 1
                Task { await model.refresh() }
            } label: {
                Label("Re-check", systemImage: "arrow.clockwise")
            }
        }
    }

    private func infoLine(_ label: String, _ value: String) -> some View {
        HStack(spacing: 8) {
            Text(label).foregroundStyle(.secondary).frame(width: 70, alignment: .leading)
            Text(value).textSelection(.enabled)
        }
    }
}
