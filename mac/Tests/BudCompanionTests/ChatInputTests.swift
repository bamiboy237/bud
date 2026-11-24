import Testing
@testable import BudCompanion

@MainActor
struct ChatInputTests {
    @Test
    func newlineAtTheCaretDoesNotMoveTheRestOfTheDraft() {
        let text = "🐶FirstSecond"
        let caret = text.index(text.startIndex, offsetBy: 6)
        let result = ChatInput.insertingNewline(in: text, replacing: caret..<caret)

        #expect(result.text == "🐶First\nSecond")
        #expect(result.text[..<result.caret] == "🐶First\n")
    }

    @Test
    func newlineReplacesSelectedText() throws {
        let text = "First replace this Last"
        let selection = try #require(text.range(of: " replace this "))
        let result = ChatInput.insertingNewline(in: text, replacing: selection)

        #expect(result.text == "First\nLast")
        #expect(result.text[result.caret...] == "Last")
    }
}
