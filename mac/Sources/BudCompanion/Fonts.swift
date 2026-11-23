import AppKit
import CoreText
import Foundation
import SwiftUI

/// Bundled optical-weight typography for bud: Inter Variable for prose/UI and IBM Plex Mono
/// (Text + Bold) for code, citations, and archival ledger metadata. Falls back safely to SF Pro
/// and SF Mono if bundled font registration or lookup ever fails.
enum Fonts {
    static let bundledFontNames = [
        "InterVariable",
        "IBMPlexMono-Text",
        "IBMPlexMono-Bold",
    ]

    static let interPostScriptName = "InterVariable"
    static let monoTextPostScriptName = "IBMPlexMono-Text"
    static let monoBoldPostScriptName = "IBMPlexMono-Bold"

    enum OpticalWeight {
        static let regular: CGFloat = 440
        static let medium: CGFloat = 530
        static let semibold: CGFloat = 590
        static let bold: CGFloat = 650
    }

    enum HeadingTracking {
        static let h1: CGFloat = -0.25
        static let h2: CGFloat = -0.15
        static let h3: CGFloat = -0.15
    }

    // OpenType variation axis tags: 'wght' and 'opsz'.
    private static let weightAxisID = 0x77676874 // 2003265652
    private static let opticalSizeAxisID = 0x6f70737a // 1869640570

    private static let registrationResult: Bool = {
        let urls = bundledFontURLs()
        guard !urls.isEmpty else { return false }
        var allRegistered = true
        for url in urls {
            var error: Unmanaged<CFError>?
            if !CTFontManagerRegisterFontsForURL(url as CFURL, .process, &error) {
                let code = error.map { CFErrorGetCode($0.takeRetainedValue()) }
                let alreadyAvailable =
                    code == CTFontManagerError.alreadyRegistered.rawValue
                    || code == CTFontManagerError.duplicatedName.rawValue
                if !alreadyAvailable {
                    allRegistered = false
                }
            }
        }
        return allRegistered
    }()

    /// Registers all bundled SIL OFL fonts for the current process. Safe to call repeatedly.
    @discardableResult
    static func register() -> Bool {
        registrationResult
    }

    /// URLs for all bundled `.ttf` font files found in the app bundle or SwiftPM resource bundle.
    static func bundledFontURLs() -> [URL] {
        bundledFontNames.compactMap { fontURL(name: $0, ext: "ttf") }
    }

    /// Resolves Inter Variable at the requested size and optical weight (`440` regular, `530`
    /// medium, `590` semibold, `650` bold), falling back to SF Pro if unavailable.
    static func sansNSFont(
        size: CGFloat,
        weight: CGFloat = OpticalWeight.regular,
        tabularDigits: Bool = false
    ) -> NSFont {
        _ = register()
        guard let base = NSFont(name: interPostScriptName, size: size) else {
            return fallbackSansNSFont(size: size, weight: weight, tabularDigits: tabularDigits)
        }
        var attributes: [NSFontDescriptor.AttributeName: Any] = [:]
        let axes = supportedAxisIDs(for: base)
        var variations: [Int: CGFloat] = [:]
        if axes.contains(weightAxisID) {
            variations[weightAxisID] = weight
        }
        if axes.contains(opticalSizeAxisID) {
            variations[opticalSizeAxisID] = min(max(size, 14), 32)
        }
        if !variations.isEmpty {
            attributes[.variation] = variations
        }
        if tabularDigits {
            attributes[.featureSettings] = tabularLiningFeatures
        }
        guard !attributes.isEmpty else { return base }
        let descriptor = base.fontDescriptor.addingAttributes(attributes)
        return NSFont(descriptor: descriptor, size: size) ?? base
    }

    /// SwiftUI `Font` for Inter Variable at the requested size and optical weight.
    static func sans(
        size: CGFloat,
        weight: CGFloat = OpticalWeight.regular,
        tabularDigits: Bool = false
    ) -> Font {
        _ = register()
        if NSFont(name: interPostScriptName, size: size) != nil {
            let resolved = sansNSFont(size: size, weight: weight, tabularDigits: tabularDigits)
            let font = Font(resolved as CTFont)
            return tabularDigits ? font.monospacedDigit() : font
        }
        let fallback = Font.system(size: size, weight: swiftUIWeight(for: weight))
        return tabularDigits ? fallback.monospacedDigit() : fallback
    }

    /// Resolves IBM Plex Mono Text (`450`) or Bold (`700`), falling back to SF Mono if unavailable.
    static func monoNSFont(
        size: CGFloat,
        bold: Bool = false,
        tabularDigits: Bool = false
    ) -> NSFont {
        _ = register()
        let name = bold ? monoBoldPostScriptName : monoTextPostScriptName
        guard let base = NSFont(name: name, size: size) else {
            return fallbackMonoNSFont(size: size, bold: bold, tabularDigits: tabularDigits)
        }
        guard tabularDigits else { return base }
        let descriptor = base.fontDescriptor.addingAttributes([
            .featureSettings: tabularLiningFeatures
        ])
        return NSFont(descriptor: descriptor, size: size) ?? base
    }

    /// SwiftUI `Font` for IBM Plex Mono Text (`450`) or Bold (`700`).
    static func mono(
        size: CGFloat,
        bold: Bool = false,
        tabularDigits: Bool = false
    ) -> Font {
        _ = register()
        let name = bold ? monoBoldPostScriptName : monoTextPostScriptName
        if NSFont(name: name, size: size) != nil {
            let resolved = monoNSFont(size: size, bold: bold, tabularDigits: tabularDigits)
            let font = Font(resolved as CTFont)
            return tabularDigits ? font.monospacedDigit() : font
        }
        let fallback = Font.system(size: size, weight: bold ? .bold : .medium, design: .monospaced)
        return tabularDigits ? fallback.monospacedDigit() : fallback
    }

    /// Editorial serif wordmark ("bud").
    static func wordmark(size: CGFloat = 13) -> Font {
        .system(size: size, weight: .semibold, design: .serif)
    }

    private static var tabularLiningFeatures: [[NSFontDescriptor.FeatureKey: Int]] {
        [
            [
                .typeIdentifier: kNumberSpacingType,
                .selectorIdentifier: kMonospacedNumbersSelector,
            ],
            [
                .typeIdentifier: kNumberCaseType,
                .selectorIdentifier: kUpperCaseNumbersSelector,
            ],
        ]
    }

    private static func supportedAxisIDs(for font: NSFont) -> Set<Int> {
        guard let axes = CTFontCopyVariationAxes(font as CTFont) as? [[String: Any]] else {
            return []
        }
        let key = kCTFontVariationAxisIdentifierKey as String
        return Set(axes.compactMap { $0[key] as? Int })
    }

    private static func fallbackSansNSFont(
        size: CGFloat,
        weight: CGFloat,
        tabularDigits: Bool
    ) -> NSFont {
        let base = NSFont.systemFont(ofSize: size, weight: appKitWeight(for: weight))
        guard tabularDigits else { return base }
        let descriptor = base.fontDescriptor.addingAttributes([
            .featureSettings: tabularLiningFeatures
        ])
        return NSFont(descriptor: descriptor, size: size) ?? base
    }

    private static func fallbackMonoNSFont(
        size: CGFloat,
        bold: Bool,
        tabularDigits: Bool
    ) -> NSFont {
        let base = NSFont.monospacedSystemFont(ofSize: size, weight: bold ? .bold : .regular)
        guard tabularDigits else { return base }
        let descriptor = base.fontDescriptor.addingAttributes([
            .featureSettings: tabularLiningFeatures
        ])
        return NSFont(descriptor: descriptor, size: size) ?? base
    }

    private static func appKitWeight(for opticalWeight: CGFloat) -> NSFont.Weight {
        if opticalWeight >= 625 { return .bold }
        if opticalWeight >= 560 { return .semibold }
        if opticalWeight >= 490 { return .medium }
        return .regular
    }

    private static func swiftUIWeight(for opticalWeight: CGFloat) -> Font.Weight {
        if opticalWeight >= 625 { return .bold }
        if opticalWeight >= 560 { return .semibold }
        if opticalWeight >= 490 { return .medium }
        return .regular
    }

    private static func fontURL(name: String, ext: String) -> URL? {
        if let appFont = Bundle.main.resourceURL?.appendingPathComponent("Fonts/\(name).\(ext)"),
           FileManager.default.fileExists(atPath: appFont.path)
        {
            return appFont
        }
        if let bundle = Bundle.main.resourceURL
            .flatMap({ Bundle(url: $0.appendingPathComponent("BudCompanion_BudCompanion.bundle")) }),
           let url = bundle.url(forResource: name, withExtension: ext)
        {
            return url
        }
        if Bundle.main.bundleURL.pathExtension != "app" {
            return Bundle.module.url(forResource: name, withExtension: ext)
        }
        return nil
    }
}
