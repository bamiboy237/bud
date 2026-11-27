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

    @Test(arguments: [
        "`[S1] [S99]`",
        "``[S1] ` [S99]``",
        "```[S1] `` [S99]```",
        "``[S1]\n[S99]``",
    ])
    func inlineCodeKeepsKnownAndUnknownMarkers(_ code: String) {
        let sources = Citations.sources(Self.raw)
        let reply = "Café 📝 [S1] \(code) then [S1] [S99]."
        #expect(Citations.linked(reply, sources: sources)
            == "Café 📝 [Nov 18](bud-source://abc123/13) \(code) then [Nov 18](bud-source://abc123/13).")
        #expect(Citations.plain(reply, sources: sources) == "Café 📝 (Nov 18) \(code) then (Nov 18).")
        #expect(Citations.plain(code, sources: [:]) == code)
    }

    @Test(arguments: [
        "```swift\nprint(\"[S1] [S99]\")\n```\n",
        "```text [S1]\n[S99]\n```\n",
        "  ````markdown\n[S1]\n```\n[S99]\n`````  \n",
        "~~~text\r\n[S1]\r\n```\r\n[S99]\r\n~~~~\r\n",
        "~~~\r[S1] [S99]\r~~~\r",
        "> ~~~text\n> [S1] [S99]\n> ~~~\n",
        "- Example\n\n  ```text\n  [S1] [S99]\n  ```\n",
    ])
    func fencedCodeKeepsMarkersAndFenceBytes(_ block: String) {
        let sources = Citations.sources(Self.raw)
        let reply = "Before [S1].\n\n\(block)\nAfter [S1] [S99]."
        #expect(Citations.linked(reply, sources: sources)
            == "Before [Nov 18](bud-source://abc123/13).\n\n\(block)\nAfter [Nov 18](bud-source://abc123/13).")
        #expect(Citations.plain(reply, sources: sources) == "Before (Nov 18).\n\n\(block)\nAfter (Nov 18).")
    }

    @Test(arguments: ["```swift\n[S1] [S99]", "~~~text\n[S1] [S99]\n"])
    func unfinishedFenceKeepsMarkersWhileStreaming(_ block: String) {
        let reply = "Before [S1].\n\n\(block)"
        let sources = Citations.sources(Self.raw)
        #expect(Citations.linked(reply, sources: sources)
            == "Before [Nov 18](bud-source://abc123/13).\n\n\(block)")
        #expect(Citations.plain(reply, sources: sources) == "Before (Nov 18).\n\n\(block)")
    }

    @Test
    func unmatchedAndEscapedBackticksLeaveProseCitationsActive() {
        let sources = Citations.sources(Self.raw)
        let reply = "Use ` or `` [S1].\n\n" + #"And \`escaped [S99]."#
        #expect(Citations.plain(reply, sources: sources) == "Use ` or `` (Nov 18).\n\n" + #"And \`escaped."#)
    }

    @Test
    func inlineCodeKeepsMarkersWhenMarkdownOffsetsShift() {
        let sources = Citations.sources(Self.raw)
        let quote = "> > > > Before [S1] ``[S1]\n> > > > [S99]`` after [S1]."
        #expect(Citations.plain(quote, sources: sources)
            == "> > > > Before (Nov 18) ``[S1]\n> > > > [S99]`` after (Nov 18).")
        let header = "| Text |\n| --- |\n"
        let cell = #"| \| \| \| \| \| \| `[S1] [S99]` then [S1]. |"#
        #expect(Citations.plain(header + cell, sources: sources)
            == header + #"| \| \| \| \| \| \| `[S1] [S99]` then (Nov 18). |"#)
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
        #expect(panel.citedTurn?.seq == 13)
        #expect(panel.citedTurn?.text == "Got it.")
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
