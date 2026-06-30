import SwiftUI

// MARK: - Color tokens

extension Color {
    static let jBg      = Color(hex: "030810")
    static let jCard    = Color(hex: "070f1c")
    static let jBorder  = Color(hex: "0e2035")
    static let jBlue    = Color(hex: "00d4ff")
    static let jBlueDim = Color(hex: "0088aa")
    static let jGreen   = Color(hex: "00aa88")
    static let jGold    = Color(hex: "f0a020")
    static let jText    = Color(hex: "cdd6e8")
    static let jDim     = Color(hex: "00d4ff").opacity(0.08)

    init(hex: String) {
        let v = UInt64(hex, radix: 16) ?? 0
        let r = Double((v >> 16) & 0xFF) / 255
        let g = Double((v >>  8) & 0xFF) / 255
        let b = Double( v        & 0xFF) / 255
        self.init(red: r, green: g, blue: b)
    }
}

// MARK: - HUD corner brackets

struct HUDCornerBrackets: View {
    var color: Color = .jBlue
    var size: CGFloat = 18
    var thickness: CGFloat = 1.5

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let h = geo.size.height
            Path { p in
                // top-left
                p.move(to:    CGPoint(x: 0,   y: size))
                p.addLine(to: CGPoint(x: 0,   y: 0))
                p.addLine(to: CGPoint(x: size, y: 0))
                // top-right
                p.move(to:    CGPoint(x: w - size, y: 0))
                p.addLine(to: CGPoint(x: w,        y: 0))
                p.addLine(to: CGPoint(x: w,        y: size))
                // bottom-left
                p.move(to:    CGPoint(x: 0,    y: h - size))
                p.addLine(to: CGPoint(x: 0,    y: h))
                p.addLine(to: CGPoint(x: size, y: h))
                // bottom-right
                p.move(to:    CGPoint(x: w - size, y: h))
                p.addLine(to: CGPoint(x: w,        y: h))
                p.addLine(to: CGPoint(x: w,        y: h - size))
            }
            .stroke(color, lineWidth: thickness)
        }
    }
}

struct HUDCorner: ViewModifier {
    func body(content: Content) -> some View {
        content.overlay(HUDCornerBrackets())
    }
}

extension View {
    func hudCorner() -> some View {
        modifier(HUDCorner())
    }
}
