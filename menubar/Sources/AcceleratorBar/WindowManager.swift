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
        dismissMenuBarPanel()
        onboarding = present(onboarding, title: "Immich Accelerator Setup",
                             view: SetupWizard(model: model))
    }

    func showSettings(model: StatusModel) {
        dismissMenuBarPanel()
        settings = present(settings, title: "Immich Accelerator Settings",
                           view: SettingsView(model: model))
    }

    // The MenuBarExtra panel sits at a high window level and does not close when
    // a button inside it is clicked, so it floats over any window we open from
    // it. Order it out explicitly. It's the key window at the moment the click
    // fires; the class-name check is a fallback for other macOS versions. Our
    // own windows are never touched. Called by every panel link (see LinkRow) so
    // clicking any action dismisses the panel like a normal menu.
    func dismissMenuBarPanel() {
        for window in NSApp.windows where window !== onboarding && window !== settings {
            let cls = String(describing: type(of: window))
            if window.isKeyWindow || cls.contains("MenuBarExtra") || cls.contains("StatusBar") {
                window.orderOut(nil)
            }
        }
    }

    private func present<V: View>(_ existing: NSWindow?, title: String, view: V) -> NSWindow {
        NSApp.setActivationPolicy(.regular)  // let the window come to the front
        let window = existing ?? make(title: title, view: view)
        Self.placeOnScreen(window)
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
        NotificationCenter.default.addObserver(
            self, selector: #selector(windowWillClose(_:)),
            name: NSWindow.willCloseNotification, object: window)
        return window
    }

    /// Put the window fully on a screen, every time it is shown.
    ///
    /// `center()` at construction was not enough, twice over. It ran before
    /// SwiftUI had applied the content's `.frame`, so it centered a window
    /// that was about to grow, leaving it hanging off the edge; and because
    /// these windows are reused (`isReleasedWhenClosed = false`), a bad frame
    /// persisted for the life of the app. Widening Settings from 460 to 700
    /// made both visible on the Mini.
    ///
    /// So: force layout first, then only move the window when it is actually
    /// off-screen. A window the user has dragged somewhere deliberate stays
    /// where they put it.
    static func placeOnScreen(_ window: NSWindow) {
        window.layoutIfNeeded()  // let the hosting controller apply its size

        guard let screen = window.screen ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        var frame = window.frame

        // A window larger than the screen can never be fully on it; shrink to
        // fit before deciding where to put it.
        frame.size.width = min(frame.width, visible.width)
        frame.size.height = min(frame.height, visible.height)

        if !visible.contains(frame) {
            // Re-center rather than nudging the nearest edge in: a frame that
            // ended up half off-screen is not a position worth preserving.
            frame.origin = CGPoint(x: visible.midX - frame.width / 2,
                                   y: visible.midY - frame.height / 2)
        }
        if frame != window.frame {
            window.setFrame(frame, display: false)
        }
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
