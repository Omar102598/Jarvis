import SwiftUI
import UIKit

// The dashboard's orb (services/dashboard/templates/voice.html): three dashed
// rings turning at different speeds, a lit quarter-arc and dots riding them,
// a sweep line while thinking. CarPlay templates can't host a SwiftUI view, so
// the arc is rendered into a looping UIImage animation for CPVoiceControlState.

enum ArcMood {
    case idle, listening, thinking, speaking

    var tint: Color { self == .listening ? .jGold : .jBlue }
    /// Seconds per loop. Each loop turns the rings a whole number of times
    /// (outer 1, middle -1, inner 2), so the animation repeats seamlessly.
    var loopSecs: Double {
        switch self {
        case .speaking: return 6
        case .thinking: return 7
        case .listening: return 9
        case .idle: return 12
        }
    }
}

struct ArcReactorView: View {
    let mood: ArcMood
    /// 0..<1 through the loop.
    let phase: Double

    var body: some View {
        Canvas { ctx, size in
            let c = CGPoint(x: size.width / 2, y: size.height / 2)
            let k = min(size.width, size.height) / 300   // the SVG is drawn on a ±150 grid
            let tint = mood.tint

            func ring(_ r: CGFloat, turn: Double, dash: [CGFloat], alpha: Double, width: CGFloat,
                      arc: Bool = false, dot: CGFloat = 0, dotAlpha: Double = 0.8) {
                var g = ctx
                g.translateBy(x: c.x, y: c.y)
                g.rotate(by: .degrees(turn * 360))
                let rr = r * k
                let circle = Path(ellipseIn: CGRect(x: -rr, y: -rr, width: rr * 2, height: rr * 2))
                g.stroke(circle, with: .color(tint.opacity(alpha)),
                         style: StrokeStyle(lineWidth: width * k, dash: dash.map { $0 * k }))
                if arc {
                    var a = Path()
                    a.addArc(center: .zero, radius: rr, startAngle: .degrees(-90),
                             endAngle: .degrees(0), clockwise: false)
                    var glow = g
                    glow.addFilter(.shadow(color: tint.opacity(0.9), radius: 4 * k))
                    glow.stroke(a, with: .color(tint.opacity(0.75)), lineWidth: 2 * k)
                }
                if dot > 0 {
                    var d = g
                    d.addFilter(.shadow(color: tint, radius: 5 * k))
                    d.fill(Path(ellipseIn: CGRect(x: rr - dot * k, y: -dot * k,
                                                  width: dot * 2 * k, height: dot * 2 * k)),
                           with: .color(tint.opacity(dotAlpha)))
                }
            }

            ring(138, turn: phase, dash: [180, 20, 60, 20, 40, 20, 80, 20], alpha: 0.30,
                 width: 1.2, arc: true, dot: 4, dotAlpha: 1)
            ring(118, turn: -phase, dash: [12, 10, 40, 10, 8, 10], alpha: 0.28, width: 1.2,
                 dot: 3, dotAlpha: 0.8)
            ring(98, turn: phase * 2, dash: [3, 22], alpha: 0.25, width: 1, dot: 2.2, dotAlpha: 0.6)

            if mood == .thinking || mood == .listening {
                var s = ctx
                s.translateBy(x: c.x, y: c.y)
                s.rotate(by: .degrees(phase * 360 * 3))
                var line = Path()
                line.move(to: .zero)
                line.addLine(to: CGPoint(x: 0, y: -130 * k))
                s.stroke(line, with: .linearGradient(
                    Gradient(colors: [tint.opacity(0), tint.opacity(0.7)]),
                    startPoint: .zero, endPoint: CGPoint(x: 0, y: -130 * k)), lineWidth: 1.5 * k)
            }

            // The core: a soft glow that breathes faster while speaking.
            let beats = mood == .speaking ? 6.0 : 2.0
            let pulse = 0.5 + 0.5 * sin(phase * .pi * 2 * beats)
            let coreR = (44 + 6 * pulse) * k
            ctx.fill(Path(ellipseIn: CGRect(x: c.x - coreR, y: c.y - coreR,
                                            width: coreR * 2, height: coreR * 2)),
                     with: .radialGradient(
                        Gradient(colors: [tint.opacity(0.55 + 0.25 * pulse), tint.opacity(0.08), .clear]),
                        center: c, startRadius: 0, endRadius: coreR))
            ctx.stroke(Path(ellipseIn: CGRect(x: c.x - 30 * k, y: c.y - 30 * k,
                                              width: 60 * k, height: 60 * k)),
                       with: .color(tint.opacity(0.5)), lineWidth: 1.5 * k)
        }
        .background(Color(red: 3/255, green: 8/255, blue: 16/255))
    }
}

enum ArcFrames {
    private static var cache: [String: UIImage] = [:]

    /// A looping animated image of the arc for `mood`, `points` wide.
    @MainActor
    static func animated(_ mood: ArcMood, points: CGFloat = 150, scale: CGFloat = 2,
                         frames: Int = 30) -> UIImage {
        let key = "\(mood)-\(points)-\(scale)-\(frames)"
        if let hit = cache[key] { return hit }
        var images: [UIImage] = []
        for i in 0..<frames {
            let r = ImageRenderer(content: ArcReactorView(mood: mood, phase: Double(i) / Double(frames))
                .frame(width: points, height: points))
            r.scale = scale
            if let img = r.uiImage { images.append(img) }
        }
        let out = UIImage.animatedImage(with: images, duration: mood.loopSecs) ?? UIImage()
        cache[key] = out
        return out
    }
}

#Preview {
    TimelineView(.animation) { t in
        let secs = t.date.timeIntervalSinceReferenceDate
        ArcReactorView(mood: .speaking, phase: secs.truncatingRemainder(dividingBy: 6) / 6)
            .frame(width: 300, height: 300)
    }
}
