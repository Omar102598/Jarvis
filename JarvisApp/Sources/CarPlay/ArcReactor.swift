import SwiftUI
import UIKit
import simd

// The dashboard's arc sphere (services/dashboard/templates/voice.html), drawn
// natively. CarPlay templates can't host a SwiftUI view, so the arc is
// rendered into a looping UIImage animation for each CPVoiceControlState.

enum ArcMood {
    case idle, listening, thinking, speaking

    /// Cyan at rest, gold while Jarvis speaks: the dashboard's arc sphere.
    var rgb: (Double, Double, Double) {
        self == .speaking ? (1.0, 0.70, 0.19) : (0.0, 0.83, 1.0)
    }
    /// Seconds per loop. Everything turns a whole number of times per loop,
    /// so the animated image repeats without a jump; a shorter loop = faster.
    var loopSecs: Double {
        switch self {
        case .speaking: return 7
        case .thinking: return 6
        case .listening: return 9
        case .idle: return 14
        }
    }
    /// How lit and "breathing" the sphere is (the dashboard's amp).
    var amp: Double {
        switch self {
        case .speaking: return 1.0
        case .listening: return 0.6
        case .thinking: return 0.35
        case .idle: return 0.08
        }
    }
}

/// A port of the dashboard's ARC SPHERE (voice.html startArc2D): ~56 broken
/// arcs on circles tilted every which way, grouped into three gimbals turning
/// about different axes, projected with perspective, crossings glowing
/// (additive blend), near arcs brighter than far ones, each arc a streak from
/// dim tail to bright head.
struct ArcSphere {
    typealias V = SIMD3<Double>
    struct Arc { let u: V; let v: V; let g: Int; let r: Double; let start: Double
                 let span: Double; let turns: Double; let w: Double; let a: Double
                 let dashed: Bool; let phase: Double }
    let arcs: [Arc]
    let chords: [(V, V, Int, Double)]
    let sparks: [(V, Int, Double, Double, Double)]
    let axes: [V] = [simd_normalize(V(0.15, 1, 0.1)), simd_normalize(V(1, 0.25, -0.2)),
                     simd_normalize(V(-0.4, 0.5, 1))]
    /// Whole turns per loop for each gimbal (signs = direction).
    let gimbalTurns: [Double] = [1, -1, 1]

    static let shared = ArcSphere()

    private init() {
        var rng = SplitMix(seed: 0x4A41525649)          // fixed: same sphere every frame
        func rnd(_ a: Double, _ b: Double) -> Double { a + rng.next() * (b - a) }
        func randUnit() -> V {
            let z = rnd(-1, 1), t = rnd(0, .pi * 2), r = (1 - z * z).squareRoot()
            return V(r * cos(t), z, r * sin(t))
        }
        var arcs: [Arc] = []
        for i in 0..<56 {
            let n = randUnit()
            let u = simd_normalize(simd_cross(n, abs(n.y) < 0.9 ? V(0, 1, 0) : V(1, 0, 0)))
            let v = simd_cross(n, u)
            let hero = i < 6
            let band = rng.next()
            let r = hero ? rnd(0.92, 1.04) : i >= 50 ? rnd(1.12, 1.26)
                  : band < 0.6 ? rnd(0.86, 1.06) : rnd(0.45, 0.85)
            arcs.append(Arc(u: u, v: v, g: i % 3, r: r, start: rnd(0, .pi * 2),
                            span: hero ? rnd(2.2, 4.6) : rnd(0.35, 2.6),
                            turns: (rng.next() < 0.5 ? 1 : -1) * (rng.next() < 0.3 ? 2 : 1),
                            w: hero ? rnd(2.0, 3.0) : i >= 50 ? rnd(0.5, 0.9) : rnd(0.6, 1.6),
                            a: hero ? 0.9 : rnd(0.35, 0.8),
                            dashed: i >= 50 || (!hero && rng.next() < 0.28),
                            phase: rnd(0, .pi * 2)))
        }
        self.arcs = arcs
        chords = (0..<10).map { _ in (randUnit(), randUnit(), Int(rnd(0, 2.99)), rnd(0.08, 0.2)) }
        sparks = (0..<140).map { _ in (randUnit(), Int(rnd(0, 2.99)), rnd(0.7, 1.08),
                                       rnd(0, .pi * 2), rnd(0.5, 1.4)) }
    }

    /// Rodrigues rotation of p about unit axis k by angle a.
    static func rot(_ p: V, _ k: V, _ a: Double) -> V {
        let c = cos(a), s = sin(a)
        return p * c + simd_cross(k, p) * s + k * (simd_dot(k, p) * (1 - c))
    }
}

/// Deterministic random numbers in 0..<1.
struct SplitMix {
    var state: UInt64
    init(seed: UInt64) { state = seed }
    mutating func next() -> Double {
        state &+= 0x9E3779B97F4A7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58476D1CE4E5B9
        z = (z ^ (z >> 27)) &* 0x94D049BB133111EB
        z ^= z >> 31
        return Double(z >> 11) / Double(1 << 53)
    }
}

struct ArcReactorView: View {
    let mood: ArcMood
    /// 0..<1 through the loop.
    let phase: Double

    var body: some View {
        Canvas { ctx, size in
            let sphere = ArcSphere.shared
            let (cr, cg, cb) = mood.rgb
            func col(_ a: Double) -> Color { Color(red: cr, green: cg, blue: cb).opacity(max(0, min(1, a))) }
            let t = phase * mood.loopSecs
            let tau = Double.pi * 2
            var env = mood.amp
            if mood == .speaking {   // the voice envelope, as on the dashboard
                let e = abs(sin(t * 3.7)) * 0.38 + abs(sin(t * 5.1)) * 0.28
                      + abs(sin(t * 2.3)) * 0.2 + abs(sin(t * 7.9)) * 0.14
                env *= 0.3 + 0.7 * min(e, 1)
            }
            let S = min(size.width, size.height) / 290
            let cx = size.width / 2, cy = size.height / 2
            let R = 104 * S * (1 + env * 0.06 + (mood == .listening ? 0.04 : 0)), F = 4.2
            let tilt = 0.32, ct = cos(tilt), st = sin(tilt)
            let angles = sphere.gimbalTurns.map { $0 * tau * phase }

            func place(_ p: ArcSphere.V, _ g: Int) -> ArcSphere.V {
                ArcSphere.rot(p, sphere.axes[g], angles[g])
            }
            func project(_ p: ArcSphere.V, _ r: Double) -> (CGPoint, Double) {
                let y = p.y * ct - p.z * st, z = p.y * st + p.z * ct
                let k = F / (F + z)
                return (CGPoint(x: cx + p.x * r * k, y: cy - y * r * k), z)
            }

            // Core bloom
            let bloomR = R * 1.25
            ctx.fill(Path(ellipseIn: CGRect(x: cx - bloomR, y: cy - bloomR, width: bloomR * 2, height: bloomR * 2)),
                     with: .radialGradient(Gradient(stops: [
                        .init(color: col(0.16 + env * 0.18), location: 0),
                        .init(color: col(0.05 + env * 0.07), location: 0.35),
                        .init(color: .clear, location: 1)]),
                        center: CGPoint(x: cx, y: cy), startRadius: 0, endRadius: bloomR))

            ctx.blendMode = .plusLighter

            for (a, b, g, al) in sphere.chords {
                let (A, _) = project(place(a, g), R * 0.98), (B, _) = project(place(b, g), R * 0.98)
                var line = Path(); line.move(to: A); line.addLine(to: B)
                ctx.stroke(line, with: .color(col(al * (0.6 + env))), lineWidth: 0.6 * S)
            }

            let chunks = 6, steps = 7
            for arc in sphere.arcs {
                let start = arc.start + arc.turns * tau * phase
                let rr = arc.r * (1 + env * 0.1 * sin(arc.phase + t * 2.4))
                for c in 0..<chunks {
                    if arc.dashed && c % 2 == 1 { continue }
                    var path = Path()
                    var zMid = 0.0
                    for k in 0...steps {
                        let th = start + arc.span * (Double(c) + Double(k) / Double(steps)) / Double(chunks)
                        let p = (arc.u * cos(th) + arc.v * sin(th)) * rr
                        let (pt, z) = project(place(p, arc.g), R)
                        if k == 0 { path.move(to: pt) } else { path.addLine(to: pt) }
                        if k == steps / 2 { zMid = z }
                    }
                    let depth = 0.45 + 0.55 * (0.5 - zMid * 0.5)        // near = brighter
                    let head = 0.25 + 0.75 * Double(c + 1) / Double(chunks)   // tail dim -> head bright
                    let alpha = arc.a * depth * head * (0.55 + env * 0.6)
                    ctx.stroke(path, with: .color(col(alpha * 0.25)),
                               style: StrokeStyle(lineWidth: arc.w * 3.2 * S, lineCap: .round))
                    ctx.stroke(path, with: .color(col(alpha)),
                               style: StrokeStyle(lineWidth: arc.w * S, lineCap: .round))
                }
            }

            for (p, g, r, tw, sz) in sphere.sparks {
                let (pt, z) = project(place(p * r, g), R)
                let twinkle = 0.5 + 0.5 * sin(tw + t * 3)
                let a = (0.15 + 0.5 * twinkle) * (0.5 - z * 0.4) * (0.5 + env)
                let d = sz * S * (1.2 - z * 0.3)
                ctx.fill(Path(ellipseIn: CGRect(x: pt.x - d / 2, y: pt.y - d / 2, width: d, height: d)),
                         with: .color(col(a)))
            }

            if mood == .thinking || mood == .listening {   // scanning sweep
                let ang = tau * phase * 3
                var line = Path()
                line.move(to: CGPoint(x: cx, y: cy))
                line.addLine(to: CGPoint(x: cx + sin(ang) * R * 1.2, y: cy - cos(ang) * R * 1.2))
                ctx.stroke(line, with: .linearGradient(Gradient(colors: [col(0), col(0.5)]),
                    startPoint: CGPoint(x: cx, y: cy),
                    endPoint: CGPoint(x: cx + sin(ang) * R * 1.2, y: cy - cos(ang) * R * 1.2)),
                    lineWidth: 1.4 * S)
            }
        }
        .background(Color(red: 3/255, green: 8/255, blue: 16/255))
    }
}

enum ArcFrames {
    private static var cache: [String: UIImage] = [:]

    /// A looping animated image of the arc for `mood`, `points` wide.
    @MainActor
    static func animated(_ mood: ArcMood, points: CGFloat = 130, scale: CGFloat = 2,
                         frames: Int = 42) -> UIImage {
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
        ArcReactorView(mood: .speaking, phase: secs.truncatingRemainder(dividingBy: 7) / 7)
            .frame(width: 300, height: 300)
    }
}
