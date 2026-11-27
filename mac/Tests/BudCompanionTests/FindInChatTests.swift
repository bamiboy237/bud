import Foundation
import Testing
@testable import BudCompanion

struct FindInChatTests {
    @Test
    func indexesEachCaseInsensitiveOccurrenceInMessageOrder() {
        let turn = ChatMessageID.turn(UUID())
        let messages: [(ChatMessageID, String)] = [
            (turn, "🐶 Bud and BUD"),
            (.latest, "Nobody here"),
            (.queued("m3"), "bud!"),
        ]
        #expect(ChatFindMatch.index(messages, query: "bUd") == [
            ChatFindMatch(message: turn, range: NSRange(location: 3, length: 3)),
            ChatFindMatch(message: turn, range: NSRange(location: 11, length: 3)),
            ChatFindMatch(message: .queued("m3"), range: NSRange(location: 0, length: 3)),
        ])
        #expect(ChatFindMatch.index(messages, query: "").isEmpty)
        #expect(ChatFindMatch.index(messages, query: "missing").isEmpty)
    }

    @Test
    func wrapsInBothDirectionsAndHandlesNoMatches() {
        #expect(ChatFindMatch.advance(from: 2, count: 3, backward: false) == 0)
        #expect(ChatFindMatch.advance(from: 0, count: 3, backward: true) == 2)
        #expect(ChatFindMatch.advance(from: 0, count: 1, backward: false) == 0)
        #expect(ChatFindMatch.advance(from: 0, count: 0, backward: true) == nil)
    }
}
