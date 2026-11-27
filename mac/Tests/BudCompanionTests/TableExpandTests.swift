import CoreGraphics
import Markdown
import Testing
@testable import BudCompanion

struct TableExpandTests {
    @Test @MainActor
    func tableIsASeparateNativeBlockBetweenProseRuns() throws {
        let markdown = """
            Before the table.

            | Year | Result |
            | --- | --- |
            | 2026 | A result wider than the header |

            After the table.
            """
        let segments = MarkdownView(markdown, rich: true).segments
        try #require(segments.count == 3)
        #expect(segments[0].nativeBlock == nil)
        #expect(segments[1].nativeBlock is Markdown.Table)
        #expect(segments[2].nativeBlock == nil)
    }

    @Test
    func edgeFadeAllowsOnePointOfRounding() {
        #expect(!MarkdownTableLayout.overflows(201, available: 200))
        #expect(MarkdownTableLayout.overflows(202, available: 200))
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
