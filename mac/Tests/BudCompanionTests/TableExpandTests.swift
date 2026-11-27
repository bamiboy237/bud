import AppKit
import Testing
@testable import BudCompanion

struct TableExpandTests {
    @Test @MainActor
    func widestRowDeterminesWhetherExpansionIsAvailable() {
        let font = NSFont.monospacedSystemFont(ofSize: 13, weight: .regular)
        let header = "Year   |   Result"
        let row = "2026   |   A result too wide for the header's viewport"
        let available = MarkdownTableLayout.textWidth(header, font: font)
        let contentWidth = MarkdownTableLayout.textWidth("\(header)\n\(row)", font: font)

        #expect(!MarkdownTableLayout.overflows(available, available: available))
        #expect(MarkdownTableLayout.overflows(contentWidth, available: available))
        #expect(!MarkdownTableLayout.overflows(contentWidth, available: contentWidth + 20))
    }

    @Test
    func theHeaderAndFirstColumnPinOnTheirOwnAxes() {
        let origin = CGPoint(x: -30, y: -50)
        #expect(MarkdownTableLayout.pinOffset(origin: origin, header: true, firstColumn: false)
                == CGSize(width: 0, height: 50))
        #expect(MarkdownTableLayout.pinOffset(origin: origin, header: false, firstColumn: true)
                == CGSize(width: 30, height: 0))
        #expect(MarkdownTableLayout.pinOffset(origin: origin, header: true, firstColumn: true)
                == CGSize(width: 30, height: 50))
        #expect(MarkdownTableLayout.pinOffset(origin: origin, header: false, firstColumn: false) == .zero)
        #expect(MarkdownTableLayout.pinOffset(
            origin: CGPoint(x: 5, y: 5), header: true, firstColumn: true) == .zero)
    }
}
