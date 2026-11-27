import AppKit
import Markdown
import Testing
@testable import BudCompanion

@MainActor
struct CrossBlockSelectionTests {
    @Test
    func quotePreservesSelectedTextAcrossMarkdownBlocksAndLeavesClipboardAlone() throws {
        let markdown = """
            A **strong** start.

            ## Heading

            - First item
            - Second item

            ```swift
            let dog = "🐶"
            ```
            """
        let segments = MarkdownView(markdown, rich: true).segments
        try #require(segments.count == 2)
        #expect(segments[0].nativeBlock == nil)
        #expect(segments[1].nativeBlock is CodeBlock)
        let view = ReplyTextView()
        let text = MarkdownView.selectableText(markdown) as NSString
        view.string = text.substring(with: segments[0].range)
        let selected = """
            start.

            Heading

            • First item
            • Second item
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

    @Test
    func findKeepsOffsetsAfterNativeBlocksAndSupplementaryCharacters() throws {
        let markdown = #"""
            🐶 Result in prose.

            ```text
            Result in code.
            ```

            $$\frac{1}{2}$$

            [Result from the web](https://example.com)

            ![Result image](https://example.com/image.png)

            Result after the cards.
            """#
        let segments = MarkdownView(markdown, rich: true).segments
        try #require(segments.count == 6)
        #expect(segments.map { $0.nativeBlock != nil } == [false, true, true, true, true, false])
        let text = MarkdownView.selectableText(markdown)
        let matches = ChatFindMatch.ranges(in: text, query: "Result")
        try #require(matches.count == 5)
        #expect(segments[0].localRange(for: matches[0]) == NSRange(location: 3, length: 6))
        #expect(segments[1].localRange(for: matches[1]) == NSRange(location: 0, length: 6))
        #expect(segments[5].localRange(for: matches[4]) == NSRange(location: 0, length: 6))
    }
}
