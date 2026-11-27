import AppKit
import SwiftUI

enum BudTheme: String, CaseIterable, Identifiable {
    case stone, studio, paper

    var id: String { rawValue }
    var title: String {
        switch self {
        case .stone: "Stone"
        case .studio: "Studio"
        case .paper: "Paper"
        }
    }
}

enum BudAppearanceMode: String, CaseIterable, Identifiable {
    case system, light, dark

    var id: String { rawValue }
    var title: String {
        switch self {
        case .system: "System"
        case .light: "Light"
        case .dark: "Dark"
        }
    }

    var colorScheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }

    var nsAppearance: NSAppearance? {
        switch self {
        case .system: nil
        case .light: NSAppearance(named: .aqua)
        case .dark: NSAppearance(named: .darkAqua)
        }
    }
}

enum BudFont: String, CaseIterable, Identifiable {
    case sfPro, inter, editorial

    var id: String { rawValue }
    var title: String {
        switch self {
        case .sfPro: "SF Pro"
        case .inter: "Inter"
        case .editorial: "Editorial"
        }
    }
}

@MainActor @Observable
final class AppearanceStore {
    static let shared = AppearanceStore()
    nonisolated(unsafe) static var activeTheme: BudTheme = .stone
    nonisolated(unsafe) static var activeFont: BudFont = .sfPro

    private let defaults: UserDefaults
    var theme: BudTheme {
        didSet {
            defaults.set(theme.rawValue, forKey: "budTheme")
            Self.activeTheme = theme
            revision += 1
        }
    }
    var mode: BudAppearanceMode {
        didSet {
            defaults.set(mode.rawValue, forKey: "budAppearanceMode")
            applyWindowAppearance()
            revision += 1
        }
    }
    var font: BudFont {
        didSet {
            defaults.set(font.rawValue, forKey: "budFont")
            Self.activeFont = font
            revision += 1
        }
    }
    private(set) var revision = 0

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        theme = defaults.string(forKey: "budTheme").flatMap(BudTheme.init(rawValue:)) ?? .stone
        mode = defaults.string(forKey: "budAppearanceMode").flatMap(BudAppearanceMode.init(rawValue:)) ?? .system
        font = defaults.string(forKey: "budFont").flatMap(BudFont.init(rawValue:)) ?? .sfPro
        Self.activeTheme = theme
        Self.activeFont = font
    }

    func applyWindowAppearance() {
        NSApp?.appearance = mode.nsAppearance
    }

    // Token reads in view bodies subscribe to changes without replacing the view hierarchy.
    nonisolated static func observeRevision() {
        if Thread.isMainThread {
            MainActor.assumeIsolated { _ = shared.revision }
        }
    }
}

enum Paper {
    // 4-step surface ladder (recessed -> canvas -> elevated -> overlay).
    static var recessed: Color {
        themed(
            stone: .init(light: 0xF2F1ED, dark: 0x141512),
            studio: .init(light: 0xF4F4F5, dark: 0x111113),
            paper: .init(light: 0xEFEFF1, dark: 0x111215, contrastLight: 0xE5E6EA, contrastDark: 0x0C0D10))
    }
    static var canvas: Color {
        themed(
            stone: .init(light: 0xFAF9F7, dark: 0x181915),
            studio: .init(light: 0xFFFFFF, dark: 0x141416),
            paper: .init(light: 0xF7F7F8, dark: 0x18191D))
    }
    static var elevated: Color {
        themed(
            stone: .init(light: 0xFFFFFF, dark: 0x1E1F1B),
            studio: .init(light: 0xFFFFFF, dark: 0x1C1C1F),
            paper: .init(light: 0xFFFFFF, dark: 0x22242A))
    }
    static var overlay: Color {
        themed(
            stone: .init(light: 0xF4F3EF, dark: 0x252622),
            studio: .init(light: 0xF7F7F8, dark: 0x242428),
            paper: .init(light: 0xFFFFFF, dark: 0x2A2D35))
    }

    // Borders.
    static var hairline: Color {
        themed(
            stone: .init(light: 0xE5E3DC, dark: 0x272825, contrastLight: 0x858278, contrastDark: 0x686960),
            studio: .init(light: 0xE4E4E7, dark: 0xFFFFFF, contrastLight: 0x81818A,
                          darkAlpha: 0.09, contrastDarkAlpha: 0.35),
            paper: .init(light: 0xE1E3E8, dark: 0xFFFFFF, contrastLight: 0x7D828C,
                         contrastDark: 0xFFFFFF, darkAlpha: 0.09, contrastDarkAlpha: 0.35))
    }
    static var borderSubtle: Color { hairline }
    static var borderStrong: Color {
        themed(
            stone: .init(light: 0xCCC9BE, dark: 0x383934, contrastLight: 0x67645A, contrastDark: 0x85867C),
            studio: .init(light: 0xCECED4, dark: 0xFFFFFF, contrastLight: 0x63636C,
                          darkAlpha: 0.16, contrastDarkAlpha: 0.48),
            paper: .init(light: 0xC2C7D0, dark: 0xFFFFFF, contrastLight: 0x5E636E,
                         contrastDark: 0xFFFFFF, darkAlpha: 0.15, contrastDarkAlpha: 0.48))
    }
    static var borderFocus: Color {
        themed(
            stone: .init(light: 0x9E9B90, dark: 0x52534C, contrastLight: 0x555249, contrastDark: 0x9A9B91),
            studio: .init(light: 0xA1A1AA, dark: 0xFFFFFF, contrastLight: 0x52525B,
                          darkAlpha: 0.26, contrastDarkAlpha: 0.58),
            paper: .init(light: 0x1E4E79, dark: 0x7BB3EB))
    }
    static var inkRule: Color { borderStrong }

    // Interactive fills & inline code pills.
    static let fillSubtle = dynamic(
        light: 0x151619, dark: 0xF0F2F5, lightAlpha: 0.045, darkAlpha: 0.055)
    static let fillHover = dynamic(
        light: 0x151619, dark: 0xF0F2F5, lightAlpha: 0.065, darkAlpha: 0.085)
    static let fillPressed = dynamic(
        light: 0x151619, dark: 0xF0F2F5, lightAlpha: 0.115, darkAlpha: 0.135)
    static let codeChipFill = dynamic(
        light: 0x151619, dark: 0xFFFFFF, lightAlpha: 0.065, darkAlpha: 0.08)
    static let codeChipBorder = dynamic(
        light: 0x151619, dark: 0xFFFFFF, lightAlpha: 0.10, darkAlpha: 0.12)

    // 4-tier ink hierarchy.
    static var inkPrimary: Color {
        themed(
            stone: .init(light: 0x181815, dark: 0xFAFAFA),
            studio: .init(light: 0x111113, dark: 0xF4F4F5),
            paper: .init(light: 0x151619, dark: 0xF0F2F5))
    }
    static var inkSecondary: Color {
        themed(
            stone: .init(light: 0x5F5E56, dark: 0x9A998F),
            studio: .init(light: 0x52525B, dark: 0xA1A1AA),
            paper: .init(light: 0x4E525B, dark: 0xA4A8B2))
    }
    static var inkMuted: Color {
        themed(
            stone: .init(light: 0x7A7970, dark: 0x7E7D74, contrastLight: 0x5F5E56, contrastDark: 0x9A998F),
            studio: .init(light: 0x71717A, dark: 0x82828B, contrastLight: 0x52525B, contrastDark: 0xA1A1AA),
            paper: .init(light: 0x626670, dark: 0x8A8F9B, contrastLight: 0x4E525B, contrastDark: 0xA4A8B2))
    }
    static var inkFaint: Color {
        themed(
            stone: .init(light: 0xA3A298, dark: 0x595852, contrastLight: 0x7A7970, contrastDark: 0x7E7D74),
            studio: .init(light: 0xA1A1AA, dark: 0x5A5A62, contrastLight: 0x71717A, contrastDark: 0x82828B),
            paper: .init(light: 0x8A8F99, dark: 0x636874, contrastLight: 0x626670, contrastDark: 0x8A8F9B))
    }
    static var placeholder: Color {
        themed(
            stone: .init(light: 0x8A897F, dark: 0x75746C),
            studio: .init(light: 0x8A8A93, dark: 0xFFFFFF, darkAlpha: 0.45),
            paper: .init(light: 0x626670, dark: 0xFFFFFF, lightAlpha: 0.88, darkAlpha: 0.48))
    }

    /// Prussian Ink: links, citations, the active pin, focus border, and cited margin bars.
    static let accent = dynamic(light: 0x1E4E79, dark: 0x7BB3EB)
    static let accentFill = accent.opacity(0.09)
    static let citationInk = dynamic(light: 0x1E4E79, dark: 0x8FC4FA)
    static let citationFill = dynamic(
        light: 0x1E4E79, dark: 0x8FC4FA, lightAlpha: 0.10, darkAlpha: 0.18)
    static let citationBorder = dynamic(
        light: 0x1E4E79, dark: 0x8FC4FA, lightAlpha: 0.28, darkAlpha: 0.35)

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
    static let radiusComposer: CGFloat = 16

    // Aliases kept for readability across existing views.
    static var surface: Color { canvas }
    static var ink: Color { inkPrimary }

    enum Typography {
        static var wordmark: Font { Fonts.wordmark(size: 13) }
        static var h1: Font { Fonts.sans(size: 20, weight: Fonts.OpticalWeight.bold) }
        static var h2: Font { Fonts.sans(size: 16.5, weight: Fonts.OpticalWeight.semibold) }
        static var h3: Font { Fonts.sans(size: 14.5, weight: Fonts.OpticalWeight.semibold) }
        static var body: Font { Fonts.sans(size: 14) }
        static var callout: Font { Fonts.sans(size: 13) }
        static var label: Font { Fonts.sans(size: 12.5, weight: Fonts.OpticalWeight.medium) }
        static var caption: Font { Fonts.sans(size: 11.5) }
        static var captionMedium: Font { Fonts.sans(size: 11.5, weight: Fonts.OpticalWeight.medium) }
        /// Archival metadata: dates, step counts, timers, language labels, "Approval 1 of 3".
        static var ledgerMeta: Font { Fonts.mono(size: 11, tabularDigits: true) }
        static var code: Font { Fonts.mono(size: 12.5) }
        static var codeBold: Font { Fonts.mono(size: 12.5, bold: true) }

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
        darkAlpha: CGFloat = 1,
        contrastLightAlpha: CGFloat? = nil,
        contrastDarkAlpha: CGFloat? = nil
    ) -> Color {
        let shades = Shades(
            light: light, dark: dark, contrastLight: contrastLight, contrastDark: contrastDark,
            lightAlpha: lightAlpha, darkAlpha: darkAlpha,
            contrastLightAlpha: contrastLightAlpha, contrastDarkAlpha: contrastDarkAlpha)
        return Color(nsColor: NSColor(name: nil) { shades.resolve(in: $0) })
    }

    private static func themed(stone: Shades, studio: Shades, paper: Shades) -> Color {
        AppearanceStore.observeRevision()
        return Color(nsColor: NSColor(name: nil) { appearance in
            let shades = switch AppearanceStore.activeTheme {
            case .stone: stone
            case .studio: studio
            case .paper: paper
            }
            return shades.resolve(in: appearance)
        })
    }

    private struct Shades: Sendable {
        var light: UInt32
        var dark: UInt32
        var contrastLight: UInt32?
        var contrastDark: UInt32?
        var lightAlpha: CGFloat = 1
        var darkAlpha: CGFloat = 1
        var contrastLightAlpha: CGFloat?
        var contrastDarkAlpha: CGFloat?

        func resolve(in appearance: NSAppearance) -> NSColor {
            let match = appearance.bestMatch(from: [
                .aqua, .darkAqua, .accessibilityHighContrastAqua,
                .accessibilityHighContrastDarkAqua,
            ])
            let hex: UInt32
            let alpha: CGFloat
            switch match {
            case .accessibilityHighContrastAqua:
                hex = contrastLight ?? light
                alpha = contrastLightAlpha ?? lightAlpha
            case .accessibilityHighContrastDarkAqua:
                hex = contrastDark ?? dark
                alpha = contrastDarkAlpha ?? darkAlpha
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
        }
    }
}

extension View {
    /// A paper surface with a hairline edge and a tight dual lift, shaped by `shape`.
    func surface<S: Shape>(
        _ shape: S,
        lift: CGFloat = 1,
        fill: Color = Paper.canvas,
        border: Color = Paper.borderSubtle,
        borderWidth: CGFloat = 0.5
    ) -> some View {
        modifier(PaperSurface(
            shape: shape, lift: lift, fill: fill, border: border, borderWidth: borderWidth))
    }
}

private struct PaperSurface<S: Shape>: ViewModifier {
    let shape: S
    let lift: CGFloat
    let fill: Color
    let border: Color
    let borderWidth: CGFloat
    @Environment(\.colorScheme) private var scheme

    func body(content: Content) -> some View {
        AppearanceStore.observeRevision()
        return content
            .background(shape.fill(fill))
            .overlay(shape.stroke(border, lineWidth: borderWidth))
            .overlay {
                if scheme == .dark && lift > 0 && AppearanceStore.activeTheme == .paper {
                    shape.stroke(.white.opacity(0.10), lineWidth: 0.5)
                }
            }
            .shadow(color: .black.opacity((scheme == .dark ? 0.22 : 0.05) * lift), radius: 1, y: 0.5)
            .shadow(color: .black.opacity((scheme == .dark ? 0.30 : 0.08) * lift), radius: 8 * lift, y: 3 * lift)
    }
}
