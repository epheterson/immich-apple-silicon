#!/usr/bin/env swift
// Generates AppIcon.icns: macOS-style rounded square, deep night gradient,
// golden bolt (the menu-bar motif). Reproducible; no design-tool artifacts.
import AppKit

func drawIcon(size: CGFloat) -> NSImage {
    let img = NSImage(size: NSSize(width: size, height: size))
    img.lockFocus()
    defer { img.unlockFocus() }

    // Canonical macOS icon grid: content square is ~80% of the canvas.
    let inset = size * 0.10
    let rect = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
    let radius = rect.width * 0.225
    let squircle = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)

    // Deep blue-black vertical gradient, subtle top glow.
    let top = NSColor(calibratedRed: 0.16, green: 0.19, blue: 0.30, alpha: 1)
    let bottom = NSColor(calibratedRed: 0.05, green: 0.06, blue: 0.12, alpha: 1)
    NSGradient(starting: top, ending: bottom)?.draw(in: squircle, angle: -90)

    // Golden bolt, SF Symbols, slight shadow for lift.
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.45)
    shadow.shadowBlurRadius = size * 0.02
    shadow.shadowOffset = NSSize(width: 0, height: -size * 0.012)
    shadow.set()

    let cfg = NSImage.SymbolConfiguration(pointSize: size * 0.42, weight: .semibold)
    guard let bolt = NSImage(systemSymbolName: "bolt.fill", accessibilityDescription: nil)?
        .withSymbolConfiguration(cfg) else { return img }
    let gold = NSColor(calibratedRed: 1.0, green: 0.80, blue: 0.25, alpha: 1)
    let tinted = NSImage(size: bolt.size)
    tinted.lockFocus()
    bolt.draw(at: .zero, from: .zero, operation: .sourceOver, fraction: 1)
    gold.set()
    NSRect(origin: .zero, size: bolt.size).fill(using: .sourceAtop)
    tinted.unlockFocus()

    let bs = bolt.size
    let scale = (rect.height * 0.60) / bs.height
    let w = bs.width * scale, h = bs.height * scale
    tinted.draw(
        in: NSRect(x: rect.midX - w / 2, y: rect.midY - h / 2, width: w, height: h),
        from: .zero, operation: .sourceOver, fraction: 1)
    return img
}

func writePNG(_ image: NSImage, px: Int, to url: URL) {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
        samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    image.draw(in: NSRect(x: 0, y: 0, width: px, height: px))
    NSGraphicsContext.restoreGraphicsState()
    try? rep.representation(using: .png, properties: [:])?.write(to: url)
}

let out = URL(fileURLWithPath: "AppIcon.iconset")
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
let master = drawIcon(size: 1024)
for (name, px) in [("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32),
                   ("icon_32x32@2x", 64), ("icon_128x128", 128), ("icon_128x128@2x", 256),
                   ("icon_256x256", 256), ("icon_256x256@2x", 512), ("icon_512x512", 512),
                   ("icon_512x512@2x", 1024)] {
    writePNG(master, px: px, to: out.appendingPathComponent("\(name).png"))
}
print("iconset written; run: iconutil -c icns AppIcon.iconset")
