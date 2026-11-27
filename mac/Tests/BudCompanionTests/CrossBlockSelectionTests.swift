import AppKit
import Testing
@testable import BudCompanion

@MainActor
struct CrossBlockSelectionTests {
    @Test
    func quotePreservesSelectedTextAcrossMarkdownBlocksAndLeavesClipboardAlone() throws {
        let markdown = """
            A **strong** start.

            - First item
            - Second item

            ```swift
            let dog = "🐶"
            ```
            """
        let view = ReplyTextView()
        view.string = MarkdownView.selectableText(markdown)
        let selected = """
            start.

            • First item
            • Second item

            let dog = "🐶"
            """
        let range = (view.string as NSString).range(of: selected)
        try #require(range.location != NSNotFound)
        view.setSelectedRange(range)

        var quote: String?
        view.onQuote = { quote = $0 }
        let clipboardChangeCount = NSPasteboard.general.changeCount
        view.quoteSelection(nil)

        #expect(quote == selected)
        #expect(NSPasteboard.general.changeCount == clipboardChangeCount)
    }
}
