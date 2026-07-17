import AppKit
import SwiftUI

// A menu-bar (LSUIElement) app has no normal windows, and SwiftUI's Window
// scenes are awkward to open from an app delegate. Hosting the onboarding and
// settings panels as plain NSWindows we own outright is simpler and reliable:
// one reusable window per role, brought to front on demand.
//
// An .accessory (menu-bar-only) app cannot activate over the frontmost app, so
// a freshly shown window opens *behind* it. We switch to .regular while a
// window is visible (so it comes to the front and takes focus) and drop back to
// .accessory once the user closes it, keeping the app menu-bar-only at rest.
@MainActor
final class WindowManager: NSObject {
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
        NSApp.setActivationPolicy(.regular)  // let the window come to the front
        let window = existing ?? make(title: title, view: view)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
        return window
    }

    private func make<V: View>(title: String, view: V) -> NSWindow {
        let window = NSWindow(contentViewController: NSHostingController(rootView: view))
        window.title = title
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false  // reuse across opens
        window.center()
        NotificationCenter.default.addObserver(
            self, selector: #selector(windowWillClose(_:)),
            name: NSWindow.willCloseNotification, object: window)
        return window
    }

    @objc private func windowWillClose(_ note: Notification) {
        // Once the closing window is gone, if none of ours remain, return to a
        // menu-bar-only presence (no dock icon).
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let stillOpen = [self.onboarding, self.settings]
                .compactMap { $0 }.contains { $0.isVisible }
            if !stillOpen { NSApp.setActivationPolicy(.accessory) }
        }
    }
}
