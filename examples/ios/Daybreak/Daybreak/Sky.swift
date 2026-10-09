import SwiftUI

/// Daybreak's sky, drawn rather than shipped as an image: a gradient, a sun on its way up, stars at night and
/// three ridges of hills. The nearest ridge is filled with `groundColor`, so whatever sits under the scene (the
/// page, or a card) runs straight on from the hills.
struct SkyScene: View {
    /// How far the sun has risen: 0 is just behind the far hills, 1 is clear of them.
    var sunrise: Double = 0.5
    /// Where the nearest ridge sits, as a fraction of the height.
    var ground: Double = 0.86
    /// The colour of the nearest ridge and everything under it.
    var groundColor: Color = Theme.background
    /// Where the sun sits across, as a fraction of the width.
    var sunX: Double = 0.68
    /// Rises the sun from just behind the hills when the scene appears.
    var risesOnAppear = false
    /// Stars in dark mode. Off where text sits on the sky.
    var showsStars = true

    @Environment(\.colorScheme) private var scheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var appeared = false

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, h = geo.size.height
            let palette = SkyPalette(scheme)
            let base = h * ground
            let lift = risesOnAppear && !appeared ? 0 : sunrise
            let radius = w * 0.075
            let sunY = base - w * 0.10 - lift * w * 0.19
            ZStack(alignment: .topLeading) {
                LinearGradient(stops: palette.sky(ground: ground), startPoint: .top, endPoint: .bottom)
                if scheme == .dark && showsStars {
                    Stars(fade: base)
                        .opacity(1 - lift * 0.45)
                }
                // The horizon's glow, wide and low, brighter as the sun clears the hills. A circle squashed
                // flat, so the gradient fades out before any edge.
                Circle()
                    .fill(RadialGradient(stops: [.init(color: palette.glow.opacity(0.8 + lift * 0.2), location: 0),
                                                 .init(color: palette.glow.opacity(0.32), location: 0.45),
                                                 .init(color: palette.glow.opacity(0), location: 1)],
                                         center: .center, startRadius: 0, endRadius: w * 0.85))
                    .frame(width: w * 1.7, height: w * 1.7)
                    .scaleEffect(x: 1, y: 0.4)
                    .position(x: w * sunX, y: base - w * 0.13)
                Sun(radius: radius, palette: palette)
                    .position(x: w * sunX, y: sunY)
                Ridge(points: Ridge.far, ground: base, width: w)
                    .fill(Ridge.shade(palette.far, top: (base - w * 0.19) / h))
                Ridge(points: Ridge.far, ground: base, width: w, open: true)
                    .stroke(palette.rim, lineWidth: 1.2)
                Ridge(points: Ridge.near, ground: base, width: w)
                    .fill(Ridge.shade(palette.near, top: (base - w * 0.09) / h))
                Ridge(points: Ridge.nearest, ground: base, width: w).fill(groundColor)
            }
            .frame(width: w, height: h)
        }
        .clipped()
        .accessibilityHidden(true)
        .onAppear {
            guard risesOnAppear, !appeared else { return }
            if reduceMotion {
                appeared = true
            } else {
                withAnimation(.spring(response: 1.4, dampingFraction: 1).delay(0.1)) { appeared = true }
            }
        }
    }
}

/// The sky's colours for one appearance: dawn by day, the last of the night in dark mode.
struct SkyPalette {
    let top: Color, upper: Color, lower: Color, horizon: Color
    let glow: Color, sunCore: Color, sunEdge: Color, rim: Color
    /// Each ridge's colour at its crest and at its foot, where the morning haze lies.
    let far: [Color], near: [Color]

    init(_ scheme: ColorScheme) {
        if scheme == .dark {
            top = Color(hex: 0x070A1F); upper = Color(hex: 0x1B1A4C); lower = Color(hex: 0x4A2F6B)
            horizon = Color(hex: 0xC9667A)
            glow = Color(hex: 0xFF8C5E)
            sunCore = Color(hex: 0xFFF0D6); sunEdge = Color(hex: 0xFFAE7A)
            rim = Color(hex: 0xFFB088, alpha: 0.45)
            far = [Color(hex: 0x40275A), Color(hex: 0x2B1C42)]
            near = [Color(hex: 0x261B3C), Color(hex: 0x19132A)]
        } else {
            top = Color(hex: 0x86A6E4); upper = Color(hex: 0xBDBDEE); lower = Color(hex: 0xF2C6CC)
            horizon = Color(hex: 0xFCD6AE)
            glow = Color(hex: 0xFFD49A)
            sunCore = Color(hex: 0xFFF9EC); sunEdge = Color(hex: 0xFFB45E)
            rim = Color(hex: 0xFFF1DC, alpha: 0.75)
            far = [Color(hex: 0xC98F98), Color(hex: 0xE2B1AA)]
            near = [Color(hex: 0xEBBDAF), Color(hex: 0xF6D9CC)]
        }
    }

    func sky(ground: Double) -> [Gradient.Stop] {
        // The brightest band sits just above the far hills, wherever the scene puts them.
        let hills = max(0.3, ground - 0.16)
        return [.init(color: top, location: 0),
                .init(color: upper, location: hills * 0.45),
                .init(color: lower, location: hills * 0.75),
                .init(color: horizon, location: hills),
                .init(color: horizon, location: 1)]
    }
}

private struct Sun: View {
    let radius: CGFloat
    let palette: SkyPalette

    var body: some View {
        ZStack {
            Circle()
                .fill(RadialGradient(colors: [palette.sunEdge.opacity(0.55), palette.sunEdge.opacity(0)],
                                     center: .center, startRadius: radius * 0.8, endRadius: radius * 3.2))
                .frame(width: radius * 6.4, height: radius * 6.4)
            Circle()
                .fill(LinearGradient(colors: [palette.sunCore, palette.sunEdge], startPoint: .top, endPoint: .bottom))
                .overlay(Circle().strokeBorder(.white.opacity(0.55), lineWidth: 1))
                .frame(width: radius * 2, height: radius * 2)
        }
    }
}

/// A line of hills through a few points, smoothed, filled down to the bottom of the scene. Each point is
/// (x as a fraction of the width, y as an offset from the ground line in widths), so hills keep their shape
/// on any height.
struct Ridge: Shape {
    let points: [CGPoint]
    let ground: CGFloat
    let width: CGFloat
    /// Only the crest line, for the rim of light along it.
    var open = false

    /// A ridge's fill: its crest colour from `top` (a fraction of the scene's height) down to its foot colour.
    static func shade(_ colors: [Color], top: CGFloat) -> LinearGradient {
        LinearGradient(colors: colors, startPoint: UnitPoint(x: 0.5, y: top), endPoint: .bottom)
    }

    static let far: [CGPoint] = [(-0.02, -0.10), (0.10, -0.15), (0.26, -0.11), (0.42, -0.17), (0.58, -0.12),
                                 (0.76, -0.18), (0.90, -0.13), (1.02, -0.16)].map(CGPoint.init)
    static let near: [CGPoint] = [(-0.02, -0.05), (0.18, -0.085), (0.38, -0.035), (0.58, -0.08), (0.80, -0.035),
                                  (1.02, -0.07)].map(CGPoint.init)
    static let nearest: [CGPoint] = [(-0.02, 0.0), (0.24, -0.03), (0.50, 0.008), (0.76, -0.022),
                                     (1.02, 0.004)].map(CGPoint.init)

    func path(in rect: CGRect) -> Path {
        let pts = points.map { CGPoint(x: rect.minX + $0.x * rect.width, y: ground + $0.y * width) }
        var path = Path()
        guard let first = pts.first else { return path }
        if open {
            path.move(to: first)
        } else {
            path.move(to: CGPoint(x: first.x, y: rect.maxY))
            path.addLine(to: first)
        }
        // Catmull-Rom through the points, as cubic Béziers.
        for i in 0..<(pts.count - 1) {
            let p0 = pts[max(i - 1, 0)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[min(i + 2, pts.count - 1)]
            path.addCurve(to: p2,
                          control1: CGPoint(x: p1.x + (p2.x - p0.x) / 6, y: p1.y + (p2.y - p0.y) / 6),
                          control2: CGPoint(x: p2.x - (p3.x - p1.x) / 6, y: p2.y - (p3.y - p1.y) / 6))
        }
        if !open {
            path.addLine(to: CGPoint(x: pts[pts.count - 1].x, y: rect.maxY))
            path.closeSubpath()
        }
        return path
    }
}

private extension CGPoint {
    init(_ pair: (Double, Double)) { self.init(x: pair.0, y: pair.1) }
}

/// The same few dozen stars every time, thinning towards the horizon.
private struct Stars: View {
    let fade: CGFloat

    var body: some View {
        Canvas { context, size in
            var seed: UInt64 = 0x2545_F491_4F6C_DD1D
            func next() -> Double {
                seed = seed &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
                return Double(seed >> 11) / Double(UInt64(1) << 53)
            }
            for _ in 0..<54 {
                let x = next() * size.width
                let y = pow(next(), 1.5) * fade * 0.8
                let r = 0.45 + next() * 0.9
                let alpha = (0.3 + next() * 0.7) * max(0, 1 - y / (fade * 0.85))
                context.fill(Path(ellipseIn: CGRect(x: x - r, y: y - r, width: r * 2, height: r * 2)),
                             with: .color(.white.opacity(alpha)))
            }
        }
    }
}

#Preview("Sky") {
    VStack(spacing: 0) {
        SkyScene(sunrise: 0.55).frame(height: 340)
        Spacer()
    }
    .background(Theme.background)
}
