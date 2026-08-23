import SwiftUI

// Design tokens for the whole app.
//
// The point is not that these particular numbers are magic. It is that there is
// one set of them. Before this file the panel used thirteen different spacing
// values, the icon column was 20pt in the menu and 16pt in Settings so nothing
// lined up across windows, and one badge used a hand-picked 9pt font. Reaching
// for a token instead of a number is what keeps a UI from drifting back into
// that, and it makes a change like "everything is a little tight" a one-line
// edit rather than an archaeology exercise.
enum Metrics {
    // 4pt grid. AppKit's own layout metrics are built on multiples of 4, so
    // anything off-grid reads as a mistake next to standard controls.
    static let xs: CGFloat = 2   // optical nudges only, never structural
    static let sm: CGFloat = 4
    static let md: CGFloat = 8
    static let lg: CGFloat = 12
    static let xl: CGFloat = 16
    static let xxl: CGFloat = 20

    /// Panel width. 300 is the narrowest that fits "1,234 processing · 5,678
    /// queued" without truncating, which is the longest line the panel shows.
    static let panelWidth: CGFloat = 300

    /// Settings window: sidebar plus detail, sized like the system's own.
    /// Fixed in both axes, because the four panes have different natural
    /// heights and letting each pick its own resizes the window under the
    /// pointer every time you switch.
    ///
    /// Height is set by the tallest pane that must not scroll. Diagnostics may
    /// run past it and scroll: it is a list of facts that grows with the number
    /// of checks, and scrolling a list is not a defect.
    static let settingsWidth: CGFloat = 700
    // Tall enough that Processing, which now carries every toggle, does
    // not open already scrolled. The window is fixed, so this is the
    // whole budget.
    static let settingsHeight: CGFloat = 640
    /// Wide enough for "Machine Learning" beside its glyph. At 190 it
    /// truncated to "Machine Le...", which is the sort of thing that only
    /// shows up when you render the window and look at it.
    static let settingsSidebarWidth: CGFloat = 215

    /// The tinted rounded square behind a sidebar glyph. 20pt with a 5pt
    /// radius is what System Settings uses; smaller reads as a bullet and
    /// larger crowds the label.
    static let paneIcon: CGFloat = 20
    static let paneIconRadius: CGFloat = 5

    /// The icon gutter. Every leading icon reserves the same width in every
    /// window, so labels form one vertical edge whether or not a given row has
    /// an icon and regardless of how wide that icon's glyph happens to be.
    static let iconColumn: CGFloat = 20
    // Tall enough for a full encode-compare report without the settings window
    // growing past its fixed height.
    static let compareResultHeight: CGFloat = 260

    /// Inset from the panel edge to content. Dividers use the same value so
    /// they start and end where the text does instead of floating.
    static let gutter: CGFloat = 14

    /// Vertical padding inside a tappable row. Below ~5 the rows read as a
    /// wall of text; above ~8 the panel gets tall enough to need scrolling.
    static let rowPadV: CGFloat = 6
    static let rowRadius: CGFloat = 6

    /// Status dot. Small enough to read as punctuation next to text rather
    /// than as a control competing with it.
    static let dot: CGFloat = 9
}

extension Font {
    /// Section and row titles. Medium weight, not bold: at this size bold
    /// competes with the window title for attention.
    static let rowTitle = Font.callout.weight(.medium)
    /// Secondary line under a title.
    static let rowDetail = Font.caption
    /// The small capsule badges (NATIVE / PYTHON). Rounded design at caption2
    /// rather than a hand-picked point size, so it scales with the user's text
    /// size setting like everything else.
    static let badge = Font.caption2.weight(.semibold).width(.condensed)
}

extension View {
    /// Standard horizontal inset for panel content.
    func panelGutter() -> some View {
        self.padding(.horizontal, Metrics.gutter)
    }
}

/// A divider inset to match the content it separates, rather than running the
/// full width of the panel and cutting the layout in half.
struct InsetDivider: View {
    var body: some View {
        Divider().padding(.horizontal, Metrics.gutter)
    }
}

/// Sets the host window's title from SwiftUI.
///
/// `navigationTitle` on a `NavigationSplitView` does not place the title the
/// same way on every macOS: on 15 it lands centered in the title bar, on 26 it
/// renders leading beside the traffic lights. Setting the NSWindow's own title
/// and letting AppKit place it is the same on both. It also works for any host,
/// which the off-screen `render` path needs.
struct WindowTitle: NSViewRepresentable {
    let title: String

    func makeNSView(context: Context) -> NSView { NSView() }

    func updateNSView(_ view: NSView, context: Context) {
        // Deferred: during an update pass the view is not in a window yet on
        // first appearance.
        let title = self.title
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            window.title = title
            // Keep the title as the window's identity (Mission Control, the
            // Window menu, screenshots) but stop the title bar drawing it, or
            // it shows twice: once leading from AppKit and once centered from
            // the .principal toolbar item.
            window.titleVisibility = .hidden
        }
    }
}

/// A settings sidebar glyph: a white SF Symbol on a tinted rounded square.
///
/// This is the detail that makes a sidebar read as a macOS settings window
/// rather than as a generic list. A bare symbol in the same slot looks like a
/// developer's list of sections; the tinted tile is what the system uses and
/// what people recognize.
struct PaneIcon: View {
    let systemName: String
    let tint: Color

    var body: some View {
        RoundedRectangle(cornerRadius: Metrics.paneIconRadius, style: .continuous)
            .fill(tint.gradient)
            .frame(width: Metrics.paneIcon, height: Metrics.paneIcon)
            .overlay(
                Image(systemName: systemName)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.white)
            )
    }
}

/// The leading icon column. One call site for size, weight, rendering mode and
/// width, so an icon can never be optically heavier in one window than another.
struct RowIcon: View {
    let systemName: String
    var active: Bool = false

    var body: some View {
        Image(systemName: systemName)
            .symbolRenderingMode(.hierarchical)
            .font(.body)
            .foregroundStyle(active ? Color.accentColor : Color.secondary)
            .frame(width: Metrics.iconColumn, alignment: .center)
    }
}
