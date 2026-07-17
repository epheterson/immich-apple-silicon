import SwiftUI

// Entry point. `status` argument = one-shot headless status dump so the state
// logic is verifiable over ssh without a GUI session.
//
// main() MUST stay synchronous and call AcceleratorBarApp.main() directly for
// the GUI. An `async main()` that wraps App.main() in MainActor.run severs the
// bridge between Swift's main-actor executor and AppKit's run loop: the scene
// renders once but Task/Timer continuations (the status poll) never drain, so
// the panel freezes on an empty snapshot. The status subcommand drives its own
// async work with a semaphore instead.
@main
struct AcceleratorBarMain {
    static func main() {
        if CommandLine.arguments.contains("status") {
            printStatus()
            return
        }
        AcceleratorBarApp.main()
    }

    // Drive the @MainActor status dump to completion without blocking the main
    // thread. A semaphore/group wait here would deadlock: textStatus() is
    // @MainActor and needs the very thread the wait would park. Pumping the
    // main run loop lets the main-actor task run, then we return. CLI-only.
    private static func printStatus() {
        nonisolated(unsafe) var output: String?
        Task { @MainActor in output = await StatusModel.textStatus() }
        while output == nil {
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        print(output ?? "")
    }
}

struct AcceleratorBarApp: App {
    @StateObject private var model = StatusModel()

    var body: some Scene {
        MenuBarExtra {
            MenuView(model: model)
        } label: {
            MenuBarLabel(model: model)
        }
        .menuBarExtraStyle(.window)
    }
}

// The menu-bar icon must live in its own @ObservedObject view. MenuBarExtra's
// `label:` closure does not reliably re-render when an @StateObject on the App
// changes, so reading `model.snap` directly in the closure leaves the icon
// frozen on its launch-time state (Stopped) even after health flips to Running.
// A dedicated observing view re-renders on every published snapshot.
struct MenuBarLabel: View {
    @ObservedObject var model: StatusModel

    var body: some View {
        Image(systemName: iconName)
            .foregroundStyle(iconColor)
    }

    // Traffic-light by glyph AND colour: green idle, amber processing, dim
    // slashed bolt stopped, orange warning degraded. The glyph alone conveys
    // state where the menu bar renders the icon monochrome.
    private var iconName: String {
        switch model.snap.overall {
        case .running: return model.snap.processing ? "bolt.fill" : "bolt.fill"
        case .stopped: return "bolt.slash"
        case .degraded: return "exclamationmark.triangle.fill"
        }
    }

    private var iconColor: Color {
        switch model.snap.overall {
        case .running: return model.snap.processing ? .yellow : .green
        case .stopped: return .secondary
        case .degraded: return .orange
        }
    }
}
