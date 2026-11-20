import AppKit
import SwiftUI

/// bud's surfaces are paper, not glass: "Cotton Stock & Prussian Ink". Neutral, cool paper so
/// the golden dog stands out, near-black ink, and one fountain-pen accent kept for links,
/// citations, the active pin, and cited margins. Colour is otherwise left to the pet.
enum Paper {
    // Surfaces, from back to front.
    static let canvas = dynamic(light: 0xF8F9FA, dark: 0x191A1D)
    static let elevated = dynamic(light: 0xFFFFFF, dark: 0x232529)
    static let recessed = dynamic(
        light: 0xEEF0F3, dark: 0x2B2D33, contrastLight: 0xE4E7EC, contrastDark: 0x353840)
    static let fillHover = dynamic(light: 0xE4E7EC, dark: 0x353840)
    static let fillPressed = dynamic(light: 0xD8DCE3, dark: 0x40444E)

    // Lines.
    static let hairline = dynamic(
        light: 0xDCE0E6, dark: 0x343740, contrastLight: 0x7D828C, contrastDark: 0x747985)
    static let inkRule = dynamic(light: 0xC2C7D0, dark: 0x4E525E)

    // Ink, by emphasis. Every text ink passes WCAG AA on every surface above.
    static let inkPrimary = dynamic(light: 0x151619, dark: 0xF0F2F5)
    static let inkSecondary = dynamic(light: 0x4E525B, dark: 0xA4A8B2)
    static let inkMuted = dynamic(
        light: 0x626670, dark: 0x9398A3, contrastLight: 0x4E525B, contrastDark: 0xA4A8B2)

    /// Prussian Ink: links, citations, the active pin, and cited margin bars. Nothing else.
    static let accent = dynamic(light: 0x1E4E79, dark: 0x7BB3EB)

    // Transient decision stamps, and genuine failures.
    static let approveInk = dynamic(light: 0x0F6B46, dark: 0x4ADE9F)
    static let approveFill = dynamic(light: 0xEAF6F0, dark: 0x163227)
    static let denyInk = dynamic(light: 0xA83224, dark: 0xF28472)
    static let denyFill = dynamic(light: 0xFCEEEB, dark: 0x3A201C)
    static let errorInk = dynamic(light: 0xB42318, dark: 0xF87171)

    // The original names, kept so every view reads the same paper.
    static let surface = canvas
    static let ink = inkPrimary

    enum Typography {
        static let wordmark = Font.system(size: 13, weight: .semibold, design: .serif)
        /// Archival metadata: dates, step counts, timers, "Approval 1 of 3".
        static let ledgerMeta = Font.system(size: 11, weight: .medium, design: .monospaced)
            .monospacedDigit()
    }

    /// A colour that follows light, dark, and Increase Contrast. Without a contrast value the
    /// normal one is used.
    static func dynamic(
        light: UInt32, dark: UInt32, contrastLight: UInt32? = nil, contrastDark: UInt32? = nil
    ) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let match = appearance.bestMatch(from: [
                .aqua, .darkAqua, .accessibilityHighContrastAqua,
                .accessibilityHighContrastDarkAqua,
            ])
            let hex: UInt32
            switch match {
            case .accessibilityHighContrastAqua: hex = contrastLight ?? light
            case .accessibilityHighContrastDarkAqua: hex = contrastDark ?? dark
            case .darkAqua: hex = dark
            default: hex = light
            }
            return NSColor(
                srgbRed: CGFloat(hex >> 16 & 0xFF) / 255, green: CGFloat(hex >> 8 & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
        })
    }
}

extension View {
    /// A paper surface with a hairline edge and a soft lift, shaped by `shape`. The lift is what
    /// separates a surface floating over other apps from whatever is behind it. `fill` picks the
    /// paper: `canvas` for windows and bubbles, `elevated` for cards and the composer.
    func surface<S: Shape>(_ shape: S, lift: CGFloat = 1, fill: Color = Paper.canvas) -> some View {
        modifier(PaperSurface(shape: shape, lift: lift, fill: fill))
    }
}

private struct PaperSurface<S: Shape>: ViewModifier {
    let shape: S
    let lift: CGFloat
    let fill: Color
    @Environment(\.colorScheme) private var scheme

    func body(content: Content) -> some View {
        content
            .background(shape.fill(fill))
            .overlay(shape.stroke(Paper.hairline, lineWidth: 0.5))
            // Dark paper catches light along its top edge; without it, a floating card sinks.
            .overlay(
                shape.stroke(.white.opacity(scheme == .dark && lift > 0 ? 0.08 : 0), lineWidth: 0.5)
                    .mask(LinearGradient(colors: [.white, .clear], startPoint: .top, endPoint: .center))
            )
            .shadow(color: .black.opacity((scheme == .dark ? 0.2 : 0.05) * lift), radius: 1, y: 0.5)
            .shadow(color: .black.opacity((scheme == .dark ? 0.3 : 0.10) * lift), radius: 16 * lift, y: 6 * lift)
    }
}
