// draw_icon.swift — draw the Thesis Ledger app icon from the UI's own tokens.
//
// The icon is a page from the ledger: warm paper, a red margin rule, three
// ruled day rows, an ink block and an oxide block. It is drawn, not generated,
// so every size is pixel-exact and the colours are the app's own.
//
// Usage: swiftc -O -swift-version 5 draw_icon.swift -o draw_icon
//        ./draw_icon out.png [size]
import AppKit
import CoreGraphics
import Foundation

// ---- tokens (mirror planner/ui/app.css) ----------------------------------
let paperTop    = CGColor(srgbRed: 0.984, green: 0.969, blue: 0.933, alpha: 1)  // #FBF7EE
let paperBottom = CGColor(srgbRed: 0.937, green: 0.906, blue: 0.843, alpha: 1)  // #EFE7D7
let ink         = CGColor(srgbRed: 0.098, green: 0.082, blue: 0.067, alpha: 1)  // #191511
let oxide       = CGColor(srgbRed: 0.690, green: 0.227, blue: 0.106, alpha: 1)  // #B03A1B
let ruleInk     = CGColor(srgbRed: 0.098, green: 0.082, blue: 0.067, alpha: 0.10)

func draw(_ size: CGFloat, compact: Bool = false) -> CGImage? {
    let cs = CGColorSpaceCreateDeviceRGB()
    guard let ctx = CGContext(data: nil, width: Int(size), height: Int(size),
                              bitsPerComponent: 8, bytesPerRow: 0, space: cs,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    else { return nil }

    // work in a 1024-unit space, y down
    let s = size / 1024.0
    ctx.scaleBy(x: s, y: s)
    ctx.translateBy(x: 0, y: 1024)
    ctx.scaleBy(x: 1, y: -1)
    ctx.setAllowsAntialiasing(true)
    ctx.interpolationQuality = .high

    // ---- squircle: Apple's grid (824 box centred in 1024) ----
    let box = CGRect(x: 100, y: 100, width: 824, height: 824)
    let radius: CGFloat = 824 * 0.2237
    let squircle = CGPath(roundedRect: box, cornerWidth: radius,
                          cornerHeight: radius, transform: nil)

    ctx.saveGState()
    ctx.addPath(squircle)
    ctx.clip()

    // paper, with a soft top-to-bottom warmth
    let gradient = CGGradient(colorsSpace: cs,
                              colors: [paperTop, paperBottom] as CFArray,
                              locations: [0, 1])!
    ctx.drawLinearGradient(gradient,
                           start: CGPoint(x: 0, y: box.minY),
                           end: CGPoint(x: 0, y: box.maxY),
                           options: [])

    // ---- the page ----
    // Four time bands, the way the week view reads: solid work, one accent
    // block, and one empty slot still waiting to be filled.
    // Below 64 px the four-band artwork turns to mush, so macOS gets a
    // simplified mark: three fatter bands, no outline, wider gaps.
    let pad: CGFloat = compact ? 150 : 128
    let left = box.minX + pad
    let width = box.width - pad * 2
    let rowH: CGFloat = compact ? 128 : 104
    let gap: CGFloat = compact ? 86 : 46
    let count: CGFloat = compact ? 3 : 4
    let top = box.minY + (box.height - (rowH * count + gap * (count - 1))) / 2

    func band(_ index: Int, _ fraction: CGFloat, _ color: CGColor?,
              outline: Bool = false) {
        let y = top + CGFloat(index) * (rowH + gap)
        let r = CGRect(x: left, y: y, width: width * fraction, height: rowH)
        let path = CGPath(roundedRect: r, cornerWidth: 13, cornerHeight: 13,
                          transform: nil)
        ctx.addPath(path)
        if let color {
            ctx.setFillColor(color)
            ctx.fillPath()
        } else {
            ctx.setStrokeColor(CGColor(srgbRed: 0.098, green: 0.082,
                                       blue: 0.067, alpha: 0.22))
            ctx.setLineWidth(5)
            ctx.strokePath()
        }
    }

    band(0, 0.72, ink)
    band(1, 0.44, oxide)
    if compact {
        band(2, 1.00, ink)
    } else {
        band(2, 1.00, ink)
        band(3, 0.30, nil, outline: true)
    }

    ctx.restoreGState()

    // a hairline edge keeps the paper from bleeding into a light Dock
    ctx.addPath(squircle)
    ctx.setStrokeColor(CGColor(srgbRed: 0.098, green: 0.082, blue: 0.067, alpha: 0.10))
    ctx.setLineWidth(3)
    ctx.strokePath()

    return ctx.makeImage()
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: draw_icon <out.png> [size]\n".data(using: .utf8)!)
    exit(2)
}
let size = CGFloat(Double(args.count >= 3 ? args[2] : "1024") ?? 1024)
let compact = args.contains("--small")
guard let image = draw(size, compact: compact) else {
    FileHandle.standardError.write("render failed\n".data(using: .utf8)!)
    exit(1)
}
let rep = NSBitmapImageRep(cgImage: image)
guard let png = rep.representation(using: .png, properties: [:]) else {
    FileHandle.standardError.write("encode failed\n".data(using: .utf8)!)
    exit(1)
}
try png.write(to: URL(fileURLWithPath: args[1]))
print("wrote \(args[1]) at \(Int(size))px")
