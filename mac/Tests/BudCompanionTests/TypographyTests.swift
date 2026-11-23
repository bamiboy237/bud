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

        let body = Fonts.sansNSFont(size: 14, weight: Fonts.OpticalWeight.regular)
        #expect(body.familyName == "Inter Variable")
        #expect(abs(body.pointSize - 14) < 0.01)
        let bodyVars = CTFontCopyVariation(body as CTFont) as? [NSNumber: NSNumber] ?? [:]
        #expect(bodyVars[NSNumber(value: 0x77676874)]?.doubleValue == 440)

        let h1 = Fonts.sansNSFont(size: 20, weight: Fonts.OpticalWeight.bold)
        #expect(h1.familyName == "Inter Variable")
        let h1Vars = CTFontCopyVariation(h1 as CTFont) as? [NSNumber: NSNumber] ?? [:]
        #expect(h1Vars[NSNumber(value: 0x77676874)]?.doubleValue == 650)
        #expect(h1Vars[NSNumber(value: 0x6f70737a)]?.doubleValue == 20)

        let monoText = Fonts.monoNSFont(size: 12.5, bold: false, tabularDigits: true)
        #expect(monoText.fontName == Fonts.monoTextPostScriptName)
        #expect(abs(monoText.pointSize - 12.5) < 0.01)

        let monoBold = Fonts.monoNSFont(size: 12.5, bold: true)
        #expect(monoBold.fontName == Fonts.monoBoldPostScriptName)

        let tabularSans = Fonts.sansNSFont(size: 14, weight: Fonts.OpticalWeight.regular, tabularDigits: true)
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
    func surfaceLadderAndSyntaxColorsResolveInLightAndDark() throws {
        for appearanceName in [NSAppearance.Name.aqua, .darkAqua] {
            let appearance = try #require(NSAppearance(named: appearanceName))
            let recessed = hex(Paper.recessed, in: appearance)
            let canvas = hex(Paper.canvas, in: appearance)
            let elevated = hex(Paper.elevated, in: appearance)

            #expect(recessed != canvas)
            #expect(canvas != elevated)
            #expect(recessed != elevated)

            if appearanceName == .aqua {
                #expect(recessed == 0xEFEFF1)
                #expect(canvas == 0xF7F7F8)
                #expect(elevated == 0xFFFFFF)
            } else {
                #expect(recessed == 0x111215)
                #expect(canvas == 0x18191D)
                #expect(elevated == 0x22242A)
                #expect(alpha(Paper.placeholder, in: appearance) >= 0.45)
                #expect(alpha(Paper.citationFill, in: appearance) >= 0.15)
                #expect(alpha(Paper.citationBorder, in: appearance) >= 0.30)
                #expect(alpha(Paper.codeChipFill, in: appearance) >= 0.07)
            }

            #expect(Syntax.color(.plain) == nil)
            for kind in [Syntax.Kind.keyword, .string, .number, .comment, .function, .type] {
                #expect(Syntax.color(kind) != nil)
            }
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
