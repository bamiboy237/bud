import Foundation
import Testing
@testable import BudCompanion

struct CitationTests {
    private static var raw: [String: Any] { [
        "S1": ["session_id": "abc123", "seq": 13, "date": "2026-11-18", "label": "Nov 18"],
        "S2": ["session_id": "abc123"],
    ] }

    @Test
    func tagsBecomeLinksAndInventedTagsDisappear() throws {
        let sources = Citations.sources(Self.raw)
        // A source missing its fields is not a source.
        #expect(Array(sources.keys) == ["S1"])
        let reply = "We chose SQLite [S1], not Postgres [S2]."
        #expect(
            Citations.linked(reply, sources: sources)
                == "We chose SQLite [Nov 18](bud-source://abc123/13), not Postgres.")
        #expect(Citations.plain(reply, sources: sources) == "We chose SQLite (Nov 18), not Postgres.")
        #expect(Citations.linked("No sources here.", sources: [:]) == "No sources here.")
    }

    @Test
    func onlySourceLinksOpenAMessage() throws {
        let target = try #require(Citations.target(URL(string: "bud-source://abc123/13")!))
        #expect(target.session == "abc123")
        #expect(target.seq == 13)
        #expect(Citations.target(URL(string: "https://example.com/13")!) == nil)
        #expect(Citations.target(URL(string: "bud-source://abc123/latest")!) == nil)
    }

    @Test
    func panelKeepsTheExchangeAroundTheCitedMessage() throws {
        let panel = try #require(SourcePanel([
            "session_id": "abc123", "seq": 13, "date": "2026-11-18",
            "turns": [
                ["role": "user", "text": "Correction: SQLite.", "seq": 11],
                ["role": "assistant", "text": "Got it.", "seq": 13],
                ["role": "assistant", "text": "", "seq": 14],
            ],
        ]))
        #expect(panel.turns.map(\.seq) == [11, 13])
        #expect(panel.turns.map(\.fromOwner) == [true, false])
        #expect(panel.date.contains("Nov 18"))
        #expect(!panel.loading)
        #expect(SourcePanel(session: "abc123", seq: 13).loading)
    }
}
