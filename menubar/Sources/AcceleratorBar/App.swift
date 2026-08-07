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
        if let i = CommandLine.arguments.firstIndex(of: "render"),
           CommandLine.arguments.count > i + 2 {
            renderSettings(pane: CommandLine.arguments[i + 1],
                           to: CommandLine.arguments[i + 2])
            return
        }
        AcceleratorBarApp.main()
    }

    /// Draw a Settings pane straight to a PNG. `render <pane> <path>`.
    ///
    /// Not a screenshot: the window renders itself into a bitmap through
    /// cacheDisplay, so this needs no Screen Recording permission and works
    /// over ssh on a headless session. Both matter here, because the Mac this
    /// is validated on refuses screen capture and the release Mini has no GUI
    /// session at all, which is why UI changes kept shipping unlooked-at.
    @MainActor
    private static func renderSettings(pane: String, to path: String) {
        setenv("ACCEL_SETTINGS_TAB", pane, 1)
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        let host = NSHostingController(rootView: SettingsView(model: .shared))
        let window = NSWindow(contentViewController: host)
        window.styleMask = [.titled, .closable]
        window.title = "Immich Accelerator Settings"
        window.orderFrontRegardless()

        // Let layout, the status poll and any async row content settle. A
        // single spin renders an empty split view.
        let deadline = Date().addingTimeInterval(3)
        while Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }

        guard let view = window.contentView,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds)
        else { print("render failed: no view"); return }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else {
            print("render failed: no png")
            return
        }
        do {
            try png.write(to: URL(fileURLWithPath: path))
            print("\(pane) -> \(path) \(rep.pixelsWide)x\(rep.pixelsHigh)")
        } catch {
            print("render failed: \(error)")
        }
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
        // Keep the core in lockstep: if this (possibly Sparkle-updated) app is
        // newer than the installed core, pull the core forward, always, no
        // prompt. So a Sparkle update upgrades the whole accelerator.
        Task { await Self.syncCoreVersion() }
    }

    @MainActor
    static func syncCoreVersion() async {
        let app = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? ""
        // Only a clean X.Y.Z release triggers this (never a "1.8.0-dev" build),
        // so local dev testing can't upgrade someone's core out from under them.
        let comps = app.split(separator: ".")
        guard comps.count == 3, comps.allSatisfy({ Int($0) != nil }) else { return }
        let core = StatusModel.readVersion()
        guard !core.isEmpty, Actions.versionNewer(app, than: core) else { return }

        // Only Homebrew installs are ours to upgrade. Someone running from
        // source has no formula to move, and shelling out to a missing brew
        // just fails on every launch.
        guard FileManager.default.isExecutableFile(atPath: Actions.brew),
              Actions.isBrewInstall else { return }

        // Ask brew whether an upgrade is actually available before running one.
        // Without this, an app that is ahead of the tap (Sparkle shipped before
        // the formula bump landed) or a deliberately pinned formula would retry
        // a full `brew upgrade` on every single launch, each one dragging a
        // `brew update` fetch along and stalling startup for seconds.
        guard let available = await Actions.coreOutdated() else {
            print("[accelerator] core \(core) is behind app \(app) but brew reports "
                  + "no upgrade available; leaving it alone")
            return
        }
        print("[accelerator] upgrading core \(core) -> \(available) to match the app")
        // brew restarts the service as part of the upgrade, which interrupts
        // in-flight jobs; they requeue on the next watch cycle.
        if await Actions.upgradeCore() {
            print("[accelerator] core upgraded")
        } else {
            print("[accelerator] core upgrade failed; will retry on next launch")
        }
        await StatusModel.shared.refresh()
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
