// text-render — draws subtitle / chapter-card / tag overlays as full-frame transparent PNGs with system fonts,
// so highlight reels can be composited by any stock ffmpeg (no libass / freetype needed).
//
//   text-render < spec.json
//   spec: {"width":1920,"height":1080,"items":[{"out":"/x/sub_001.png","kind":"sub"|"card"|"tag"|"blank",
//          "lines":["主行","second line"]}]}
//
// Style must match static/app.css (.ov-a / .ov-b): sizes in 1080p units, outline drawn OUTSIDE the glyphs
// (outline pass first, clean fill on top — like libass / CSS paint-order: stroke fill).
import AppKit
import Foundation

struct Item: Decodable { let out: String; let kind: String; let lines: [String] }
struct Spec: Decodable { let width: Int; let height: Int; let items: [Item]; let han: String? }
var hanVariant = "TC"  // Chinese glyph forms for Han-only lines: TC (Taiwan) / SC (mainland) / HK

let outlineColor = NSColor(white: 0.05, alpha: 1)
let pale = NSColor(red: 0.85, green: 0.9, blue: 0.94, alpha: 1)        // #d9e6f0
let yellow = NSColor(red: 1, green: 0.84, blue: 0.04, alpha: 1)         // #ffd60a
let spokenColor = yellow                                                // karaoke: already-spoken words

/// Font per line by script: kana → Hiragino (Japanese glyphs), Hangul → Apple SD Gothic Neo, otherwise PingFang
/// in the project's Chinese variant (Latin text looks the same in all of them).
func font(_ size: CGFloat, _ weight: NSFont.Weight, for text: String = "") -> NSFont {
    let bold = weight != .regular
    var kana = false, hangul = false
    for u in text.unicodeScalars {
        switch u.value {
        case 0x3040...0x30FF: kana = true
        case 0xAC00...0xD7AF, 0x1100...0x11FF, 0x3130...0x318F: hangul = true
        default: break
        }
    }
    let name: String
    if kana { name = bold ? "HiraginoSans-W6" : "HiraginoSans-W3" }
    else if hangul { name = bold ? "AppleSDGothicNeo-Bold" : "AppleSDGothicNeo-Regular" }
    else { name = "PingFang\(hanVariant)-" + (bold ? "Semibold" : "Regular") }
    return NSFont(name: name, size: size) ?? NSFont.systemFont(ofSize: size, weight: weight)
}

func para(_ align: NSTextAlignment, _ size: CGFloat) -> NSParagraphStyle {
    let ps = NSMutableParagraphStyle()
    ps.alignment = align
    ps.lineBreakMode = .byWordWrapping
    ps.lineSpacing = size * 0.08
    return ps
}

/// Fill-only text (no outline, no shadow).
func fill(_ s: String, size: CGFloat, weight: NSFont.Weight, color: NSColor,
          align: NSTextAlignment = .center, script: String? = nil) -> NSAttributedString {
    NSAttributedString(string: s, attributes: [.font: font(size, weight, for: script ?? s), .foregroundColor: color,
                                               .paragraphStyle: para(align, size)])
}

/// Draws `s` with an outline of `outline` × font size visible OUTSIDE the glyphs, plus a soft shadow.
func drawOutlined(_ s: NSAttributedString, in rect: NSRect, outline: CGFloat, shadow: Bool = true) {
    if outline > 0 {
        let o = NSMutableAttributedString(attributedString: s)
        let all = NSRange(location: 0, length: o.length)
        o.addAttribute(.foregroundColor, value: NSColor.clear, range: all)
        o.addAttribute(.strokeColor, value: outlineColor, range: all)
        // strokeWidth is a % of the font size, centred on the glyph edge → ×2 so the outer half == outline
        o.addAttribute(.strokeWidth, value: outline * 200, range: all)
        if shadow {
            let sh = NSShadow()
            sh.shadowColor = NSColor(white: 0, alpha: 0.6)
            let size = (s.attribute(.font, at: 0, effectiveRange: nil) as? NSFont)?.pointSize ?? 40
            sh.shadowBlurRadius = size * 0.08
            sh.shadowOffset = NSSize(width: 0, height: -size * 0.03)
            o.addAttribute(.shadow, value: sh, range: all)
        }
        o.draw(with: rect, options: [.usesLineFragmentOrigin])
    }
    s.draw(with: rect, options: [.usesLineFragmentOrigin])
}

func height(_ s: NSAttributedString, width: CGFloat) -> CGFloat {
    ceil(s.boundingRect(with: NSSize(width: width, height: 10_000), options: [.usesLineFragmentOrigin]).height)
}

func render(_ it: Item, w: Int, h: Int) throws {
    let W = CGFloat(w), H = CGFloat(h), u = H / 1080
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    NSColor.clear.set()
    NSRect(x: 0, y: 0, width: W, height: H).fill()
    let margin = W * 0.07, tw = W - 2 * margin
    switch it.kind {
    case "sub":
        // bottom-centred: line 1 = 56 white semibold, line 2 = 38 pale regular (same as .ov-a / .ov-b)
        var y = 46 * u
        for (i, line) in it.lines.enumerated().reversed() where !line.isEmpty {
            let size = (i == 0 ? 56 : 38) * u
            let weight: NSFont.Weight = i == 0 ? .semibold : .regular
            let base: NSColor = i == 0 ? .white : pale
            // karaoke markup: "\u{1}" starts and "\u{2}" ends a run of already-spoken words
            let s = NSMutableAttributedString()
            var run = "", lit = false
            func flush() {
                if !run.isEmpty { s.append(fill(run, size: size, weight: weight, color: lit ? spokenColor : base, script: line)) }
                run = ""
            }
            for ch in line {
                if ch == "\u{1}" { flush(); lit = true } else if ch == "\u{2}" { flush(); lit = false } else { run.append(ch) }
            }
            flush()
            let hgt = height(s, width: tw)
            drawOutlined(s, in: NSRect(x: margin, y: y, width: tw, height: hgt), outline: 0.05)
            y += hgt + 6 * u
        }
    case "card":
        let title = fill(it.lines.first ?? "", size: 96 * u, weight: .semibold, color: yellow)
        let sub = fill(it.lines.count > 1 ? it.lines[1] : "", size: 40 * u, weight: .regular, color: .white)
        let th = height(title, width: tw), sh = height(sub, width: tw)
        let total = th + 18 * u + sh
        let top = (H + total) / 2
        drawOutlined(title, in: NSRect(x: margin, y: top - th, width: tw, height: th), outline: 0.04)
        drawOutlined(sub, in: NSRect(x: margin, y: top - total, width: tw, height: sh), outline: 0.05)
    case "tag":
        let s = fill(it.lines.first ?? "", size: 34 * u, weight: .semibold, color: .black, align: .left)
        let r = s.boundingRect(with: NSSize(width: W * 0.6, height: H), options: [.usesLineFragmentOrigin])
        let pad = 14 * u
        let box = NSRect(x: 40 * u, y: H - 40 * u - r.height - 2 * pad, width: r.width + 2 * pad, height: r.height + 2 * pad)
        yellow.withAlphaComponent(0.95).set()
        NSBezierPath(roundedRect: box, xRadius: 8 * u, yRadius: 8 * u).fill()
        s.draw(with: NSRect(x: box.minX + pad, y: box.minY + pad, width: r.width + 2, height: ceil(r.height)),
               options: [.usesLineFragmentOrigin])
    default: break  // blank
    }
    NSGraphicsContext.restoreGraphicsState()
    guard let png = rep.representation(using: .png, properties: [:]) else { throw NSError(domain: "png", code: 1) }
    try png.write(to: URL(fileURLWithPath: it.out))
}

let data = FileHandle.standardInput.readDataToEndOfFile()
do {
    let spec = try JSONDecoder().decode(Spec.self, from: data)
    hanVariant = ["TC", "SC", "HK"].contains(spec.han ?? "") ? spec.han! : "TC"
    for it in spec.items { try render(it, w: spec.width, h: spec.height) }
    print("rendered \(spec.items.count)")
} catch {
    FileHandle.standardError.write("ERROR \(error)\n".data(using: .utf8)!)
    exit(1)
}
