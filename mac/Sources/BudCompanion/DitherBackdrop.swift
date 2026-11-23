import AppKit
import CoreGraphics
import SwiftUI

/// Subtle editorial pixel-matrix backdrop: an 8×8 Bayer ordered-dithering matrix applied to a
/// continuous, fluid 24fps traveling organic wave field, rendered directly into a compact cell-grid
/// `CGImage` where each pixel draws as a crisp `3pt × 3pt` square dot (`.interpolation(.none)`).
/// Pauses automatically when the window/app is inactive or Reduce Motion is enabled, and hides
/// when Increase Contrast is on.
struct DitherBackdrop: View {
    static let cellPoints: CGFloat = 3
    static let frameInterval: TimeInterval = 1.0 / 24.0
    private static let epoch = Date(timeIntervalSinceReferenceDate: 0)

    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.colorSchemeContrast) private var contrast
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.controlActiveState) private var controlActiveState

    private var paused: Bool {
        reduceMotion || controlActiveState == .inactive
    }

    var body: some View {
        GeometryReader { proxy in
            if contrast != .increased {
                TimelineView(
                    .animation(minimumInterval: Self.frameInterval, paused: paused)
                ) { context in
                    let time = paused ? 0 : context.date.timeIntervalSince(Self.epoch)
                    if let image = Self.renderImage(
                        for: proxy.size,
                        scheme: colorScheme,
                        time: time)
                    {
                        Image(decorative: image, scale: 1, orientation: .up)
                            .resizable()
                            .interpolation(.none)
                            .frame(width: proxy.size.width, height: proxy.size.height)
                    }
                }
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    /// Standard 8×8 Bayer ordered-dithering threshold matrix (`0..<64`).
    private static let bayer8x8: [Double] = [
         0, 48, 12, 60,  3, 51, 15, 63,
        32, 16, 44, 28, 35, 19, 47, 31,
         8, 56,  4, 52, 11, 59,  7, 55,
        40, 24, 36, 20, 43, 27, 39, 23,
         2, 50, 14, 62,  1, 49, 13, 61,
        34, 18, 46, 30, 33, 17, 45, 29,
        10, 58,  6, 54,  9, 57,  5, 53,
        42, 26, 38, 22, 41, 25, 37, 21,
    ]

    /// Renders a compact `(cols × rows)` cell-grid `CGImage` for `size` where each pixel maps to
    /// one crisp `3pt × 3pt` square dot when drawn with `.interpolation(.none)`.
    static func renderImage(
        for size: CGSize,
        scheme: ColorScheme,
        time: TimeInterval = 0
    ) -> CGImage? {
        guard size.width >= 16, size.height >= 16 else { return nil }
        let cols = max(1, Int(ceil(size.width / cellPoints)))
        let rows = max(1, Int(ceil(size.height / cellPoints)))
        let isDark = scheme == .dark

        // Premultiplied RGBA bytes for an active dot:
        // - Dark mode: #FFFFFF at ~6.6% alpha (17 / 255) over #18191D canvas
        // - Light mode: #151619 at ~5.1% alpha (13 / 255) over #F7F7F8 canvas
        let dotR: UInt8
        let dotG: UInt8
        let dotB: UInt8
        let dotA: UInt8
        if isDark {
            dotA = 17
            dotR = 17
            dotG = 17
            dotB = 17
        } else {
            dotA = 13
            dotR = UInt8((21 * Int(dotA)) / 255)
            dotG = UInt8((22 * Int(dotA)) / 255)
            dotB = UInt8((25 * Int(dotA)) / 255)
        }

        var pixels = [UInt8](repeating: 0, count: cols * rows * 4)
        let invCols = 1.0 / Double(max(1, cols - 1))
        let invRows = 1.0 / Double(max(1, rows - 1))

        for cy in 0..<rows {
            let v = Double(cy) * invRows
            let bayerRowOffset = (cy & 7) * 8
            let rowBase = cy * cols * 4
            for cx in 0..<cols {
                let u = Double(cx) * invCols
                let intensity = waveField(u: u, v: v, time: time)
                let threshold = (bayer8x8[bayerRowOffset + (cx & 7)] + 0.5) / 64.0
                guard intensity > threshold else { continue }

                let idx = rowBase + cx * 4
                pixels[idx] = dotR
                pixels[idx + 1] = dotG
                pixels[idx + 2] = dotB
                pixels[idx + 3] = dotA
            }
        }

        guard let provider = CGDataProvider(data: Data(pixels) as CFData),
              let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)
        else { return nil }

        return CGImage(
            width: cols,
            height: rows,
            bitsPerComponent: 8,
            bitsPerPixel: 32,
            bytesPerRow: cols * 4,
            space: colorSpace,
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
            provider: provider,
            decode: nil,
            shouldInterpolate: false,
            intent: .defaultIntent
        )
    }

    /// Continuous traveling organic wave luminance field in `0...0.76`: 3 domain-warped wave
    /// octaves drift across the viewport at a calm meditative pace (`~0.30 rad/s`), keeping
    /// ~45–55% of the surface open solid canvas while soft halftone crests roll across.
    static func waveField(u: Double, v: Double, time: Double = 0) -> Double {
        let t = time * 0.30
        let w1 = sin(u * 3.4 + v * 2.6 - t)
        let w2 = cos(u * 2.8 - v * 3.6 + t * 0.82 + 0.7 * w1)
        let w3 = sin((u + v) * 4.5 - t * 1.15 + 0.6 * w2)

        let dx = u - 0.52
        let dy = v - 0.50
        let edgeBias = min(0.22, (dx * dx + dy * dy) * 0.55)

        let combined = 0.44 * w1 + 0.34 * w2 + 0.22 * w3 + edgeBias
        let lifted = (combined - 0.04) * 0.82
        guard lifted > 0 else { return 0 }
        let clamped = min(lifted, 1.0)
        return clamped * clamped * (3.0 - 2.0 * clamped) * 0.76
    }
}
