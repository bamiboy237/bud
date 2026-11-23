import AppKit
import SwiftUI

/// bud's surfaces are paper, not glass: "Cotton Stock & Prussian Ink". Neutral, cool paper so
/// the golden dog stands out, near-black ink, and one fountain-pen accent kept for links,
/// citations, the active pin, and cited margins. Colour is otherwise left to the pet.
enum Paper {
    // 4-step surface ladder (recessed -> canvas -> elevated -> overlay).
    static let recessed = dynamic(
        light: 0xEFEFF1, dark: 0x141518, contrastLight: 0xE5E6EA, contrastDark: 0x101114)
    static let canvas = dynamic(light: 0xF7F7F8, dark: 0x191A1D)
    static let elevated = dynamic(light: 0xFFFFFF, dark: 0x222429)
    static let overlay = dynamic(light: 0xFFFFFF, dark: 0x292C33)

    // Borders.
    static let hairline = dynamic(
        light: 0xE1E3E8, dark: 0x2F323A, contrastLight: 0x7D828C, contrastDark: 0x747985)
    static let borderSubtle = hairline
    static let borderStrong = dynamic(
        light: 0xC2C7D0, dark: 0x464A56, contrastLight: 0x5E636E, contrastDark: 0x8E94A2)
    static let borderFocus = dynamic(light: 0x1E4E79, dark: 0x7BB3EB)
    static let inkRule = borderStrong

    // Interactive fills.
    static let fillSubtle = dynamic(
        light: 0x151619, dark: 0xF0F2F5, lightAlpha: 0.045, darkAlpha: 0.055)
    static let fillHover = dynamic(
        light: 0x151619, dark: 0xF0F2F5, lightAlpha: 0.065, darkAlpha: 0.08)
    static let fillPressed = dynamic(
        light: 0x151619, dark: 0xF0F2F5, lightAlpha: 0.115, darkAlpha: 0.13)

    // 4-tier ink hierarchy.
    static let inkPrimary = dynamic(light: 0x151619, dark: 0xF0F2F5)
    static let inkSecondary = dynamic(light: 0x4E525B, dark: 0xA4A8B2)
    static let inkMuted = dynamic(
        light: 0x626670, dark: 0x8A8F9B, contrastLight: 0x4E525B, contrastDark: 0xA4A8B2)
    static let inkFaint = dynamic(
        light: 0x8A8F99, dark: 0x636874, contrastLight: 0x626670, contrastDark: 0x8A8F9B)

    /// Prussian Ink: links, citations, the active pin, focus border, and cited margin bars.
    static let accent = dynamic(light: 0x1E4E79, dark: 0x7BB3EB)
    static let accentFill = accent.opacity(0.09)
    static let focusHalo = accent.opacity(0.16)

    // Transient decision stamps, and genuine failures.
    static let approveInk = dynamic(light: 0x0F6B46, dark: 0x4ADE9F)
    static let approveFill = dynamic(light: 0xEAF6F0, dark: 0x163227)
    static let denyInk = dynamic(light: 0xA83224, dark: 0xF28472)
    static let denyFill = dynamic(light: 0xFCEEEB, dark: 0x3A201C)
    static let errorInk = dynamic(light: 0xB42318, dark: 0xF87171)

    // Tight radii.
    static let radiusXS: CGFloat = 4
    static let radiusSM: CGFloat = 6
    static let radiusMD: CGFloat = 10
    static let radiusLG: CGFloat = 12

    // Aliases kept for readability across existing views.
    static let surface = canvas
    static let ink = inkPrimary

    enum Typography {
        static let wordmark = Fonts.wordmark(size: 13)
        static let h1 = Fonts.sans(size: 20, weight: Fonts.OpticalWeight.bold)
        static let h2 = Fonts.sans(size: 16.5, weight: Fonts.OpticalWeight.semibold)
        static let h3 = Fonts.sans(size: 14.5, weight: Fonts.OpticalWeight.semibold)
        static let body = Fonts.sans(size: 14, weight: Fonts.OpticalWeight.regular)
        static let callout = Fonts.sans(size: 13, weight: Fonts.OpticalWeight.regular)
        static let label = Fonts.sans(size: 12.5, weight: Fonts.OpticalWeight.medium)
        static let caption = Fonts.sans(size: 11.5, weight: Fonts.OpticalWeight.regular)
        static let captionMedium = Fonts.sans(size: 11.5, weight: Fonts.OpticalWeight.medium)
        /// Archival metadata: dates, step counts, timers, language labels, "Approval 1 of 3".
        static let ledgerMeta = Fonts.mono(size: 11, bold: false, tabularDigits: true)
        static let code = Fonts.mono(size: 12.5, bold: false)
        static let codeBold = Fonts.mono(size: 12.5, bold: true)

        static let h1Tracking = Fonts.HeadingTracking.h1
        static let h2Tracking = Fonts.HeadingTracking.h2
        static let h3Tracking = Fonts.HeadingTracking.h3
    }

    /// A colour that follows light, dark, and Increase Contrast. Without a contrast value the
    /// normal one is used.
    static func dynamic(
        light: UInt32,
        dark: UInt32,
        contrastLight: UInt32? = nil,
        contrastDark: UInt32? = nil,
        lightAlpha: CGFloat = 1,
        darkAlpha: CGFloat = 1
    ) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let match = appearance.bestMatch(from: [
                .aqua, .darkAqua, .accessibilityHighContrastAqua,
                .accessibilityHighContrastDarkAqua,
            ])
            let hex: UInt32
            let alpha: CGFloat
            switch match {
            case .accessibilityHighContrastAqua:
                hex = contrastLight ?? light
                alpha = lightAlpha
            case .accessibilityHighContrastDarkAqua:
                hex = contrastDark ?? dark
                alpha = darkAlpha
            case .darkAqua:
                hex = dark
                alpha = darkAlpha
            default:
                hex = light
                alpha = lightAlpha
            }
            return NSColor(
                srgbRed: CGFloat(hex >> 16 & 0xFF) / 255,
                green: CGFloat(hex >> 8 & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255,
                alpha: alpha)
        })
    }
}

extension View {
    /// A paper surface with a hairline edge and a tight dual lift, shaped by `shape`.
    func surface<S: Shape>(
        _ shape: S,
        lift: CGFloat = 1,
        fill: Color = Paper.canvas,
        border: Color = Paper.borderSubtle
    ) -> some View {
        modifier(PaperSurface(shape: shape, lift: lift, fill: fill, border: border))
    }
}

private struct PaperSurface<S: Shape>: ViewModifier {
    let shape: S
    let lift: CGFloat
    let fill: Color
    let border: Color
    @Environment(\.colorScheme) private var scheme

    func body(content: Content) -> some View {
        content
            .background(shape.fill(fill))
            .overlay(shape.stroke(border, lineWidth: 0.5))
            // Dark paper catches light along its top edge; without it, a floating card sinks.
            .overlay(
                shape.stroke(.white.opacity(scheme == .dark && lift > 0 ? 0.08 : 0), lineWidth: 0.5)
                    .mask(LinearGradient(colors: [.white, .clear], startPoint: .top, endPoint: .center))
            )
            .shadow(color: .black.opacity((scheme == .dark ? 0.22 : 0.05) * lift), radius: 1, y: 0.5)
            .shadow(color: .black.opacity((scheme == .dark ? 0.30 : 0.08) * lift), radius: 8 * lift, y: 3 * lift)
    }
}
