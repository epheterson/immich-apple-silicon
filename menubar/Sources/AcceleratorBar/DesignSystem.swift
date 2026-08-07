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
    static let settingsWidth: CGFloat = 460

    /// The icon gutter. Every leading icon reserves the same width in every
    /// window, so labels form one vertical edge whether or not a given row has
    /// an icon and regardless of how wide that icon's glyph happens to be.
    static let iconColumn: CGFloat = 20

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
