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
        // `probe-library <path>` exposes the library check to the shell, the
        // same reason `status` and `render` exist: the wizard's answers should
        // be verifiable on a real machine without driving the GUI.
        if let i = CommandLine.arguments.firstIndex(of: "probe-library"),
           CommandLine.arguments.count > i + 1 {
            let path = CommandLine.arguments[i + 1]
            let sem = DispatchSemaphore(value: 0)
            nonisolated(unsafe) var out = ""
            Task {
                let r = await Actions.probeLibrary(path)
                out = "\(r.ok ? "OK" : "NO") \(r.note)"
                sem.signal()
            }
            while sem.wait(timeout: .now()) == .timedOut {
                RunLoop.main.run(until: Date().addingTimeInterval(0.02))
            }
            print(out)
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
    ///
    /// Known limit: over ssh there is no window server to composite vibrancy,
    /// so material-backed views (the sidebar) come out blank and the title bar
    /// may lay out differently. Trust a headless render for *content* and a
    /// render on a real display for *layout*.
    @MainActor
    private static func renderSettings(pane: String, to path: String) {
        setenv("ACCEL_SETTINGS_TAB", pane, 1)
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        // "wizard" renders the setup flow instead of Settings, so the first-run
        // experience can be looked at on the release machine too. It is the
        // screen most users see exactly once and we see least often.
        // "wizard:where" renders that step directly; see WizardModel.init.
        if pane.hasPrefix("wizard:") {
            setenv("ACCEL_WIZARD_STEP", String(pane.dropFirst("wizard:".count)), 1)
        }
        let host: NSViewController = pane.hasPrefix("wizard")
            ? NSHostingController(rootView: SetupWizard(model: .shared))
            : NSHostingController(rootView: SettingsView(model: .shared))
        let window = NSWindow(contentViewController: host)
        window.styleMask = [.titled, .closable]
        window.title = "Immich Accelerator Settings"
        // Parked far off-screen and never ordered front. This used to call
        // orderFrontRegardless, which put a real window on whatever display
        // the machine had; on the Mini that meant watching windows blink in
        // and out, half off the bottom of the screen, every time this ran.
        // Dropping orderFrontRegardless alone was not enough: the window still
        // materialized at a negative origin (measured at 959,-216 on the
        // Mini's 1920x1050 display). cacheDisplay needs the window laid out,
        // not visible, so put it somewhere it cannot be seen and leave it
        // there.
        window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        window.layoutIfNeeded()

        // Let layout, the status poll and any async row content settle. A
        // single spin renders an empty split view.
        let deadline = Date().addingTimeInterval(3)
        while Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }

        // The theme frame (contentView's superview), not the content view, so
        // the capture includes the title bar. Without it this tool could not
        // answer questions about the window title, which is one of the things
        // it exists to check.
        guard let content = window.contentView,
              let view = content.superview ?? content as NSView?,
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
