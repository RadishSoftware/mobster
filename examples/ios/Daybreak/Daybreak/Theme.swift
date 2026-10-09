import SwiftUI
import UIKit

/// First light: warm paper by day, a violet night by dark, and one ember accent, the colour of the sun
/// at the horizon. Every colour has a light and a dark value.
enum Theme {
    /// The page.
    static let background = Color(light: 0xFAF6F1, dark: 0x0E0C15)
    /// Cards and grouped rows.
    static let surface = Color(light: 0xFFFFFF, dark: 0x1A1722)
    static let ink = Color(light: 0x1C1A22, dark: 0xF6F1EB)
    static let secondary = Color(light: 0x1C1A22, dark: 0xF6F1EB, lightAlpha: 0.62, darkAlpha: 0.64)
    static let tertiary = Color(light: 0x1C1A22, dark: 0xF6F1EB, lightAlpha: 0.40, darkAlpha: 0.40)
    static let hairline = Color(light: 0x1C1A22, dark: 0xFFFFFF, lightAlpha: 0.08, darkAlpha: 0.09)
    /// Ember: selection, the badge, finished habits.
    static let accent = Color(light: 0xC9532F, dark: 0xFF9468)
    static let accentSoft = Color(light: 0xC9532F, dark: 0xFF9468, lightAlpha: 0.10, darkAlpha: 0.15)
    static let onAccent = Color(light: 0xFFFFFF, dark: 0x1C1A22)
    /// A picked card: the faintest ember wash by day, a lifted surface at night (an ember tint turns brown there).
    static let selectedSurface = Color(light: 0xFFF8F4, dark: 0x221E2B)
    static let accentGlow = Color(light: 0xC9532F, dark: 0xFF9468, lightAlpha: 0.16, darkAlpha: 0.22)
    /// The edge of a card that has to hold its shape against the page.
    static let cardEdge = Color(light: 0x1C1A22, dark: 0xFFFFFF, lightAlpha: 0.10, darkAlpha: 0.13)
    /// The one main action on a screen: ink by day, warm cream at night.
    static let action = Color(light: 0x1C1A22, dark: 0xFFF1E3)
    static let onAction = Color(light: 0xFFFFFF, dark: 0x1C1A22)
    /// A soft shadow under cards by day. Dark mode separates with the surface colour alone.
    static let shadow = Color(light: 0x3B2A1E, dark: 0x000000, lightAlpha: 0.08, darkAlpha: 0)
}

extension Color {
    /// A colour with a value for each appearance, from 0xRRGGBB.
    init(light: UInt32, dark: UInt32, lightAlpha: CGFloat = 1, darkAlpha: CGFloat = 1) {
        self.init(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? UIColor(hex: dark, alpha: darkAlpha) : UIColor(hex: light, alpha: lightAlpha)
        })
    }

    init(hex: UInt32, alpha: Double = 1) {
        self.init(uiColor: UIColor(hex: hex, alpha: alpha))
    }
}

extension UIColor {
    convenience init(hex: UInt32, alpha: CGFloat = 1) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
    }
}

/// The full-width button for the one main action on a screen.
struct PrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .foregroundStyle(Theme.onAction)
            .frame(maxWidth: .infinity)
            .frame(height: 56)
            .background(Capsule().fill(Theme.action))
            .opacity(isEnabled ? 1 : 0.35)
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.spring(response: 0.3, dampingFraction: 1), value: configuration.isPressed)
    }
}

/// The quieter full-width button beside a primary one.
struct SecondaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .foregroundStyle(isEnabled ? Theme.ink : Theme.secondary)
            .frame(maxWidth: .infinity)
            .frame(height: 56)
            .background(Capsule().fill(Theme.surface))
            .overlay(Capsule().strokeBorder(Theme.hairline, lineWidth: 1))
            .shadow(color: Theme.shadow, radius: 10, y: 3)
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.spring(response: 0.3, dampingFraction: 1), value: configuration.isPressed)
    }
}

/// Anything that shrinks a little under the finger.
struct PressableStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.spring(response: 0.3, dampingFraction: 1), value: configuration.isPressed)
    }
}

extension View {
    /// A white card with a soft shadow by day, a raised surface at night.
    func surface(cornerRadius: CGFloat = 22) -> some View {
        background(
            RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                .fill(Theme.surface)
                .shadow(color: Theme.shadow, radius: 14, y: 4)
        )
        .overlay(
            RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                .strokeBorder(Theme.hairline, lineWidth: 0.5)
        )
    }

    /// Glass on iOS 26, a thin material before it.
    @ViewBuilder
    func glass<S: Shape>(in shape: S) -> some View {
        if #available(iOS 26.0, *) {
            glassEffect(.regular.interactive(), in: shape)
        } else {
            background(.ultraThinMaterial, in: shape)
        }
    }
}
