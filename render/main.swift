// text-render — draws subtitle / chapter-card / tag overlays as full-frame transparent PNGs with system fonts,
// so highlight reels can be composited by any stock ffmpeg (no libass / freetype needed).
//
//   text-render < spec.json
//   spec: {"width":1920,"height":1080,"items":[{"out":"/x/sub_001.png","kind":"sub"|"card"|"tag"|"blank",
//          "lines":["主行","second line"]}]}
import AppKit
import Foundation

struct Item: Decodable { let out: String; let kind: String; let lines: [String] }
struct Spec: Decodable { let width: Int; let height: Int; let items: [Item] }

func font(_ size: CGFloat, _ weight: NSFont.Weight) -> NSFont {
    let name = weight == .black ? "PingFangTC-Semibold" : weight == .bold ? "PingFangTC-Semibold" : "PingFangTC-Regular"
    return NSFont(name: name, size: size) ?? NSFont.systemFont(ofSize: size, weight: weight)
}

func attr(_ s: String, size: CGFloat, weight: NSFont.Weight, color: NSColor, stroke: CGFloat = 0,
          align: NSTextAlignment = .center, shadow: Bool = true) -> NSAttributedString {
    let ps = NSMutableParagraphStyle()
    ps.alignment = align
    ps.lineBreakMode = .byWordWrapping
    ps.lineSpacing = size * 0.08
    var a: [NSAttributedString.Key: Any] = [.font: font(size, weight), .foregroundColor: color, .paragraphStyle: ps]
    if stroke > 0 {
        a[.strokeColor] = NSColor(white: 0.05, alpha: 1)
        a[.strokeWidth] = -stroke
    }
    if shadow {
        let sh = NSShadow()
        sh.shadowColor = NSColor(white: 0, alpha: 0.85)
        sh.shadowBlurRadius = size * 0.12
        sh.shadowOffset = NSSize(width: 0, height: -size * 0.04)
        a[.shadow] = sh
    }
    return NSAttributedString(string: s, attributes: a)
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
    let margin = W * 0.07
    let yellow = NSColor(red: 1, green: 0.84, blue: 0.04, alpha: 1)
    switch it.kind {
    case "sub":
        // bottom-centred: first line large white, second (translation / original) smaller pale
        var y = 46 * u
        for (i, line) in it.lines.enumerated().reversed() where !line.isEmpty {
            let s = attr(line, size: (i == 0 ? 56 : 38) * u, weight: i == 0 ? .bold : .regular,
                         color: i == 0 ? .white : NSColor(red: 0.85, green: 0.9, blue: 0.94, alpha: 1), stroke: 3.2)
            let r = s.boundingRect(with: NSSize(width: W - 2 * margin, height: H), options: [.usesLineFragmentOrigin])
            s.draw(with: NSRect(x: margin, y: y, width: W - 2 * margin, height: ceil(r.height)),
                   options: [.usesLineFragmentOrigin])
            y += ceil(r.height) + 6 * u
        }
    case "card":
        let title = attr(it.lines.first ?? "", size: 96 * u, weight: .black, color: yellow, stroke: 2.5)
        let sub = attr(it.lines.count > 1 ? it.lines[1] : "", size: 40 * u, weight: .regular, color: .white)
        let tr = title.boundingRect(with: NSSize(width: W - 2 * margin, height: H), options: [.usesLineFragmentOrigin])
        let sr = sub.boundingRect(with: NSSize(width: W - 2 * margin, height: H), options: [.usesLineFragmentOrigin])
        let total = tr.height + 18 * u + sr.height
        let top = (H + total) / 2
        title.draw(with: NSRect(x: margin, y: top - tr.height, width: W - 2 * margin, height: ceil(tr.height)),
                   options: [.usesLineFragmentOrigin])
        sub.draw(with: NSRect(x: margin, y: top - total, width: W - 2 * margin, height: ceil(sr.height)),
                 options: [.usesLineFragmentOrigin])
    case "tag":
        let s = attr(it.lines.first ?? "", size: 34 * u, weight: .bold, color: .black, align: .left, shadow: false)
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
    for it in spec.items { try render(it, w: spec.width, h: spec.height) }
    print("rendered \(spec.items.count)")
} catch {
    FileHandle.standardError.write("ERROR \(error)\n".data(using: .utf8)!)
    exit(1)
}
