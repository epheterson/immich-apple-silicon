import AppKit
import SwiftUI

// A menu-bar (LSUIElement) app has no normal windows, and SwiftUI's Window
// scenes are awkward to open from an app delegate. Hosting the onboarding and
// settings panels as plain NSWindows we own outright is simpler and reliable:
// one reusable window per role, brought to front on demand.
@MainActor
final class WindowManager {
    static let shared = WindowManager()

    private var onboarding: NSWindow?
    private var settings: NSWindow?

    func showOnboarding(model: StatusModel) {
        onboarding = present(onboarding, title: "Immich Accelerator Setup",
                             view: OnboardingView(model: model))
    }

    func showSettings(model: StatusModel) {
        settings = present(settings, title: "Immich Accelerator Settings",
                           view: SettingsView(model: model))
    }

    private func present<V: View>(_ existing: NSWindow?, title: String, view: V) -> NSWindow {
        if let window = existing {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return window
        }
        let window = NSWindow(contentViewController: NSHostingController(rootView: view))
        window.title = title
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false  // reuse across opens
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        return window
    }
}
