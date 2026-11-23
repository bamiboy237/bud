import AppKit
import CoreGraphics
import SwiftUI

/// Subtle editorial pixel-matrix backdrop: an 8×8 Bayer ordered-dithering matrix applied to an
/// organic sweeping wave luminance field, rendered into a cached ring of monochrome `CGImage`
/// frames of crisp `2pt` square pixel dots. Drifts slowly like calm ocean waves over a seamless
/// 15-second periodic loop with ~0% CPU after cache warmup, pauses automatically when the window
/// or app is inactive or when Reduce Motion is enabled, and hides when Increase Contrast is on.
struct DitherBackdrop: View {
    static let frameCount = 24
    static let loopDuration: TimeInterval = 15.0

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
                    .animation(
                        minimumInterval: Self.loopDuration / Double(Self.frameCount),
                        paused: paused)
                ) { context in
                    let idx = paused ? 0 : Self.frameIndex(at: context.date)
                    if let image = Self.cachedImage(
                        for: proxy.size, scheme: colorScheme, frameIndex: idx)
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

    static func frameIndex(at date: Date) -> Int {
        let elapsed = date.timeIntervalSinceReferenceDate
        let cycle = (elapsed / loopDuration).truncatingRemainder(dividingBy: 1.0)
        let normalized = cycle < 0 ? cycle + 1.0 : cycle
        return Int(normalized * Double(frameCount)) % frameCount
    }

    static func phase(forFrame frameIndex: Int) -> Double {
        let wrapped = ((frameIndex % frameCount) + frameCount) % frameCount
        return (2.0 * .pi * Double(wrapped)) / Double(frameCount)
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

    private final class Box {
        let image: CGImage
        init(_ image: CGImage) { self.image = image }
    }

    nonisolated(unsafe) private static let cache: NSCache<NSString, Box> = {
        let cache = NSCache<NSString, Box>()
        cache.countLimit = 128
        return cache
    }()

    /// Returns a cached `CGImage` for the quantized surface size, appearance, and loop frame.
    static func cachedImage(
        for size: CGSize,
        scheme: ColorScheme,
        frameIndex: Int = 0
    ) -> CGImage? {
        guard size.width >= 16, size.height >= 16 else { return nil }
        // Quantize to 32pt buckets so window resizing reuses buckets smoothly without churn.
        let bucketW = max(32, Int((size.width / 32).rounded(.up)) * 32)
        let bucketH = max(32, Int((size.height / 32).rounded(.up)) * 32)
        let isDark = scheme == .dark
        let wrappedFrame = ((frameIndex % frameCount) + frameCount) % frameCount
        let key = "\(bucketW)x\(bucketH)-\(isDark ? "dark" : "light")-f\(wrappedFrame)" as NSString
        if let hit = cache.object(forKey: key) {
            return hit.image
        }
        guard let rendered = renderImage(
            widthPoints: bucketW,
            heightPoints: bucketH,
            isDark: isDark,
            phase: phase(forFrame: wrappedFrame)
        ) else {
            return nil
        }
        cache.setObject(Box(rendered), forKey: key)
        return rendered
    }

    /// Renders crisp `2pt × 2pt` dither dots into a premultiplied RGBA `CGImage`.
    static func renderImage(
        widthPoints: Int,
        heightPoints: Int,
        isDark: Bool,
        phase: Double = 0
    ) -> CGImage? {
        let cellPt = 2
        let cols = max(8, widthPoints / cellPt)
        let rows = max(8, heightPoints / cellPt)
        let pixelW = cols * cellPt
        let pixelH = rows * cellPt

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

        var pixels = [UInt8](repeating: 0, count: pixelW * pixelH * 4)
        let invCols = 1.0 / Double(max(1, cols - 1))
        let invRows = 1.0 / Double(max(1, rows - 1))

        for cy in 0..<rows {
            let v = Double(cy) * invRows
            let bayerRowOffset = (cy & 7) * 8
            for cx in 0..<cols {
                let u = Double(cx) * invCols
                let intensity = waveField(u: u, v: v, phase: phase)
                let threshold = (bayer8x8[bayerRowOffset + (cx & 7)] + 0.5) / 64.0
                guard intensity > threshold else { continue }

                // Fill the 2pt × 2pt square dot.
                let px0 = cx * cellPt
                let py0 = cy * cellPt
                for dy in 0..<cellPt {
                    let rowBase = ((py0 + dy) * pixelW + px0) * 4
                    for dx in 0..<cellPt {
                        let idx = rowBase + dx * 4
                        pixels[idx] = dotR
                        pixels[idx + 1] = dotG
                        pixels[idx + 2] = dotB
                        pixels[idx + 3] = dotA
                    }
                }
            }
        }

        guard let provider = CGDataProvider(data: Data(pixels) as CFData),
              let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)
        else { return nil }

        return CGImage(
            width: pixelW,
            height: pixelH,
            bitsPerComponent: 8,
            bitsPerPixel: 32,
            bytesPerRow: pixelW * 4,
            space: colorSpace,
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
            provider: provider,
            decode: nil,
            shouldInterpolate: false,
            intent: .defaultIntent
        )
    }

    /// Smooth organic wave luminance field in `0...0.82` with seamless `2π`-periodic `phase`:
    /// strongest along a curved diagonal wave band sweeping from upper-left through center-left,
    /// feathering smoothly into open canvas toward the right and bottom-right.
    static func waveField(u: Double, v: Double, phase: Double = 0) -> Double {
        let ridge1 = 0.24 + 0.16 * sin(v * .pi * 1.35 + 0.35 + phase) - 0.10 * v
        let ridge2 = 0.46 + 0.14 * cos(v * .pi * 1.75 - 0.40 - phase) - 0.14 * v
        let ripple =
            0.055 * sin(u * 7.2 + v * 4.4 - phase)
            + 0.04 * cos(u * 4.6 - v * 6.2 + phase)

        let d1 = (u - ridge1 + ripple) / 0.26
        let band1 = exp(-d1 * d1 * 1.35)

        let d2 = (u - ridge2 - ripple * 0.7) / 0.22
        let band2 = 0.62 * exp(-d2 * d2 * 1.6)

        let cornerCanopy = max(0, 1.0 - hypot(u * 1.15, v * 1.25))
        let feather = pow(max(0, 1.0 - 0.56 * u - 0.32 * v), 1.25)

        let raw = (0.72 * band1 + 0.48 * band2 + 0.36 * cornerCanopy) * feather
        let contour = 0.88 + 0.12 * sin((u * 3.2 + v * 2.4 + band1 * 1.4) * .pi * 2.0 - phase)
        return min(max(raw * contour, 0), 0.82)
    }
}
