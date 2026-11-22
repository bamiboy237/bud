import Foundation
import Testing
@testable import BudCompanion

struct CitationTests {
    private static var raw: [String: Any] { [
        "S1": ["session_id": "abc123", "seq": 13, "date": "2026-11-18", "label": "Nov 18"],
        "S2": ["session_id": "abc123"],
        "S3": [
            "kind": "workspace",
            "path": "reports/2026-11-18-job-1-weekly.md",
            "date": "2026-11-18",
            "label": "reports/2026-11-18-job-1-weekly.md",
        ],
    ] }

    @Test
    func tagsBecomeLinksAndInventedTagsDisappear() throws {
        let sources = Citations.sources(Self.raw)
        // A source missing its fields is not a source.
        #expect(Set(sources.keys) == ["S1", "S3"])
        let reply = "We chose SQLite [S1], not Postgres [S2]. See report [S3]."
        #expect(
            Citations.linked(reply, sources: sources)
                == "We chose SQLite [Nov 18](bud-source://abc123/13), not Postgres. See report [reports/2026-11-18-job-1-weekly.md](bud-source://workspace?path=reports/2026-11-18-job-1-weekly.md).")
        #expect(
            Citations.plain(reply, sources: sources)
                == "We chose SQLite (Nov 18), not Postgres. See report (reports/2026-11-18-job-1-weekly.md).")
        #expect(Citations.linked("No sources here.", sources: [:]) == "No sources here.")
    }

    @Test
    func onlySourceLinksOpenAMessageOrWorkspaceFile() throws {
        let turn = try #require(Citations.target(URL(string: "bud-source://abc123/13")!))
        #expect(turn == .turn(session: "abc123", seq: 13))
        let file = try #require(
            Citations.target(URL(string: "bud-source://workspace?path=reports/weekly.md")!))
        #expect(file == .workspace(path: "reports/weekly.md"))
        #expect(Citations.target(URL(string: "https://example.com/13")!) == nil)
        #expect(Citations.target(URL(string: "bud-source://abc123/latest")!) == nil)
        #expect(Citations.target(URL(string: "bud-source://workspace")!) == nil)
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
        #expect(SourcePanel(target: .turn(session: "abc123", seq: 13)).loading)
    }

    @Test
    func panelRendersWorkspaceDocument() throws {
        let panel = try #require(SourcePanel([
            "kind": "workspace",
            "path": "notes/plan.md",
            "date": "2026-11-18",
            "content": "# Plan\n\nShip SQLite.",
        ]))
        #expect(panel.target == .workspace(path: "notes/plan.md"))
        #expect(panel.document == "# Plan\n\nShip SQLite.")
        #expect(panel.date.contains("Nov 18"))
        #expect(!panel.loading)
    }
}
