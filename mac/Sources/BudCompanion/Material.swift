import AppKit
import SwiftUI

/// bud's surfaces are paper, not glass: a warm off-white with near-black ink, and the reverse in
/// dark mode. Colour is left to the pet, so the interface itself stays quiet.
enum Paper {
    static let surface = dynamic(light: 0xFBFAF7, dark: 0x22211F)
    static let ink = dynamic(light: 0x1C1B1B, dark: 0xEDEBE6)
    static let hairline = ink.opacity(0.12)

    private static func dynamic(light: UInt32, dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let hex = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
            return NSColor(
                srgbRed: CGFloat(hex >> 16 & 0xFF) / 255, green: CGFloat(hex >> 8 & 0xFF) / 255,
                blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
        })
    }
}

extension View {
    /// A paper surface with a hairline edge and a soft lift, shaped by `shape`. The lift is what
    /// separates a surface floating over other apps from whatever is behind it.
    func surface<S: Shape>(_ shape: S, lift: CGFloat = 1) -> some View {
        background(shape.fill(Paper.surface))
            .overlay(shape.stroke(Paper.hairline, lineWidth: 0.5))
            .shadow(color: .black.opacity(0.05 * lift), radius: 1, y: 0.5)
            .shadow(color: .black.opacity(0.10 * lift), radius: 16 * lift, y: 6 * lift)
    }
}
