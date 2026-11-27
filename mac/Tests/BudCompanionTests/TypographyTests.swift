import AppKit
import CoreText
import SwiftUI
import Testing
@testable import BudCompanion

@MainActor
struct TypographyTests {
    @Test
    func bundledFontsExistInModuleAndRegisterCleanly() throws {
        for name in Fonts.bundledFontNames {
            let url = try #require(
                Bundle.module.url(forResource: name, withExtension: "ttf"),
                "Expected \(name).ttf in Bundle.module")
            let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
            let size = try #require(attrs[.size] as? NSNumber).intValue
            #expect(size > 10_000, "\(name).ttf should be a non-empty font file")
        }

        let urls = Fonts.bundledFontURLs()
        #expect(urls.count == Fonts.bundledFontNames.count)

        #expect(Fonts.register())
        #expect(Fonts.register())
    }

    @Test
    func bundledFontsResolveOpticalWeightsAndMonoCuts() throws {
        #expect(Fonts.register())

        let body = Fonts.sansNSFont(size: 14, weight: Fonts.OpticalWeight.regular, family: .inter)
        #expect(body.familyName == "Inter Variable")
        #expect(abs(body.pointSize - 14) < 0.01)
        let bodyVars = CTFontCopyVariation(body as CTFont) as? [NSNumber: NSNumber] ?? [:]
        #expect(bodyVars[NSNumber(value: 0x77676874)]?.doubleValue == 440)

        let h1 = Fonts.sansNSFont(size: 20, weight: Fonts.OpticalWeight.bold, family: .inter)
        #expect(h1.familyName == "Inter Variable")
        let h1Vars = CTFontCopyVariation(h1 as CTFont) as? [NSNumber: NSNumber] ?? [:]
        #expect(h1Vars[NSNumber(value: 0x77676874)]?.doubleValue == 650)
        #expect(h1Vars[NSNumber(value: 0x6f70737a)]?.doubleValue == 20)

        for (weight, expected) in [(Fonts.OpticalWeight.medium, 530.0), (Fonts.OpticalWeight.semibold, 590.0)] {
            let font = Fonts.sansNSFont(size: 14, weight: weight, family: .inter)
            let variations = CTFontCopyVariation(font as CTFont) as? [NSNumber: NSNumber]
            #expect(variations?[NSNumber(value: 0x77676874)]?.doubleValue == expected)
        }

        let monoText = Fonts.monoNSFont(size: 12.5, bold: false, tabularDigits: true, family: .inter)
        #expect(monoText.fontName == Fonts.monoTextPostScriptName)
        #expect(abs(monoText.pointSize - 12.5) < 0.01)

        let monoBold = Fonts.monoNSFont(size: 12.5, bold: true, family: .inter)
        #expect(monoBold.fontName == Fonts.monoBoldPostScriptName)

        let tabularSans = Fonts.sansNSFont(size: 14, tabularDigits: true, family: .inter)
        let sansFeatures = tabularSans.fontDescriptor.object(forKey: .featureSettings) as? [[String: Any]]
        #expect(sansFeatures?.contains {
            ($0[kCTFontFeatureTypeIdentifierKey as String] as? Int) == kNumberSpacingType
                && ($0[kCTFontFeatureSelectorIdentifierKey as String] as? Int) == kMonospacedNumbersSelector
        } == true)
        #expect(abs(lineWidth("111", font: tabularSans) - lineWidth("888", font: tabularSans)) < 0.01)
        #expect(abs(lineWidth("111", font: monoText) - lineWidth("888", font: monoText)) < 0.01)
    }

    private func lineWidth(_ string: String, font: NSFont) -> CGFloat {
        let attr = NSAttributedString(string: string, attributes: [.font: font])
        let line = CTLineCreateWithAttributedString(attr)
        return CGFloat(CTLineGetTypographicBounds(line, nil, nil, nil))
    }

    @Test
    func typographyAndRadiusTokensMatchInvariants() {
        #expect(Paper.Typography.h1Tracking == -0.25)
        #expect(Paper.Typography.h2Tracking == -0.15)
        #expect(Paper.Typography.h3Tracking == -0.15)

        #expect(Paper.radiusXS == 4)
        #expect(Paper.radiusSM == 6)
        #expect(Paper.radiusMD == 10)
        #expect(Paper.radiusLG == 12)

        let page = ReplyType(size: 14, compact: false)
        #expect(page.codeSize == 12.5)
        #expect(page.tracking(for: 1) == -0.25)
        #expect(page.tracking(for: 2) == -0.15)
        #expect(page.tracking(for: 3) == -0.15)
        #expect(page.tracking(for: 4) == 0)
    }

    @Test
    func appearanceDefaultsToStoneSystemAndSFPro() throws {
        _ = AppearanceStore.shared
        let previousTheme = AppearanceStore.activeTheme
        let previousFont = AppearanceStore.activeFont
        let suite = "BudCompanionTests.Appearance.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer {
            AppearanceStore.activeTheme = previousTheme
            AppearanceStore.activeFont = previousFont
            defaults.removePersistentDomain(forName: suite)
        }

        let appearance = AppearanceStore(defaults: defaults)
        #expect(appearance.theme == .stone)
        #expect(appearance.mode == .system)
        #expect(appearance.font == .sfPro)
        #expect(AppearanceStore.activeTheme == .stone)
        #expect(AppearanceStore.activeFont == .sfPro)
    }

    @Test
    func surfaceLaddersResolveInLightAndDark() throws {
        _ = AppearanceStore.shared
        let previousTheme = AppearanceStore.activeTheme
        defer { AppearanceStore.activeTheme = previousTheme }

        let light = try #require(NSAppearance(named: .aqua))
        let dark = try #require(NSAppearance(named: .darkAqua))
        let ladders: [(theme: BudTheme, light: [UInt32], dark: [UInt32])] = [
            (.stone, [0xF2F1ED, 0xFAF9F7, 0xFFFFFF], [0x141512, 0x181915, 0x1E1F1B]),
            // Studio light uses white for both the canvas and elevated surfaces.
            (.studio, [0xF4F4F5, 0xFFFFFF, 0xFFFFFF], [0x111113, 0x141416, 0x1C1C1F]),
            (.paper, [0xEFEFF1, 0xF7F7F8, 0xFFFFFF], [0x111215, 0x18191D, 0x22242A]),
        ]
        for ladder in ladders {
            AppearanceStore.activeTheme = ladder.theme
            let surfaces = [Paper.recessed, Paper.canvas, Paper.elevated]
            #expect(surfaces.map { hex($0, in: light) } == ladder.light)
            #expect(surfaces.map { hex($0, in: dark) } == ladder.dark)
        }

        AppearanceStore.activeTheme = .paper
        #expect(abs(alpha(Paper.placeholder, in: dark) - 0.48) < 0.01)
        #expect(alpha(Paper.citationFill, in: dark) >= 0.15)
        #expect(alpha(Paper.citationBorder, in: dark) >= 0.30)
        #expect(alpha(Paper.codeChipFill, in: dark) >= 0.07)

        #expect(Syntax.color(.plain) == nil)
        for kind in [Syntax.Kind.keyword, .string, .number, .comment, .function, .type] {
            #expect(Syntax.color(kind) != nil)
        }
    }

    @Test
    func ditherBackdropGeneratesTravelingWaveFramesInLightAndDark() throws {
        let size = CGSize(width: 520, height: 640)
        let expectedCols = Int(ceil(size.width / DitherBackdrop.cellPoints))
        let expectedRows = Int(ceil(size.height / DitherBackdrop.cellPoints))

        for scheme in [ColorScheme.light, .dark] {
            let frame0 = try #require(
                DitherBackdrop.renderImage(for: size, scheme: scheme, time: 0))
            #expect(frame0.width == expectedCols)
            #expect(frame0.height == expectedRows)

            let data0 = try #require(frame0.dataProvider?.data) as Data
            let activeCells = stride(from: 3, to: data0.count, by: 4).reduce(into: 0) { count, idx in
                if data0[idx] != 0 { count += 1 }
            }
            let totalCells = expectedCols * expectedRows
            // Traveling wave crests leave substantial open solid canvas while rendering visible halftone crests.
            #expect(activeCells > totalCells / 12)
            #expect(activeCells < (totalCells * 3) / 5)

            // Distinct timestamps produce distinct traveling wave frames.
            let frameLater = try #require(
                DitherBackdrop.renderImage(for: size, scheme: scheme, time: 3.5))
            let dataLater = try #require(frameLater.dataProvider?.data) as Data
            #expect(dataLater != data0)
        }
    }

    private func alpha(_ color: Color, in appearance: NSAppearance) -> CGFloat {
        var resolved: CGFloat = 0
        appearance.performAsCurrentDrawingAppearance {
            let srgb = NSColor(color).usingColorSpace(.sRGB) ?? NSColor(color)
            resolved = srgb.alphaComponent
        }
        return resolved
    }

    private func hex(_ color: Color, in appearance: NSAppearance) -> UInt32 {
        var resolved: UInt32 = 0
        appearance.performAsCurrentDrawingAppearance {
            let srgb = NSColor(color).usingColorSpace(.sRGB) ?? NSColor(color)
            let r = UInt32((srgb.redComponent * 255).rounded())
            let g = UInt32((srgb.greenComponent * 255).rounded())
            let b = UInt32((srgb.blueComponent * 255).rounded())
            resolved = (r << 16) | (g << 8) | b
        }
        return resolved
    }
}
