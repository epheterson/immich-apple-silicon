import AppKit
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
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = StatusModel.shared

    var body: some Scene {
        MenuBarExtra {
            MenuView(model: model)
        } label: {
            MenuBarLabel(model: model)
        }
        .menuBarExtraStyle(.window)
    }
}

// On first launch, if the accelerator isn't set up yet, open the onboarding
// window so a fresh install has a path forward instead of a dead menu bar.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Dev/test affordance: open Settings straight away so it can be captured
        // headlessly (screenshots) without driving the menu-bar panel.
        if ProcessInfo.processInfo.environment["ACCEL_SHOW_SETTINGS"] != nil {
            WindowManager.shared.showSettings(model: .shared)
            return
        }
        if !Paths.isConfigured {
            WindowManager.shared.showOnboarding(model: .shared)
        }
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
    }

    // Monochrome by design: the menu bar templates the icon so it adapts to
    // light/dark and matches the other status items. State is carried by the
    // glyph; the idle/processing detail and job counts live in the panel.
    private var iconName: String {
        switch model.snap.overall {
        case .running: return "bolt.fill"
        case .stopped: return "bolt.slash"
        case .degraded: return "exclamationmark.triangle.fill"
        }
    }
}
