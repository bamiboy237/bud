import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct TurnProvenanceTests {
    private let payload = """
    {
      "web_sources": [
        {"title":"Schedule","url":"https://example.com/schedule","domain":"example.com","excerpt":"Friday","status":"read"},
        {"title":"Agenda","url":"https://example.org/agenda","domain":"example.org","excerpt":"","status":"found"}
      ],
      "approval_receipts": [
        {"approval_id":"a1","label":"Send email","decision":"approved","decided_at":"2026-12-01T18:30:00.123456+00:00","status":"confirmed"},
        {"approval_id":"a2","label":"Post update","decision":"denied","decided_at":"2026-12-01T18:31:00+00:00","status":"denied"},
        {"approval_id":"a3","label":"Send draft","decision":"approved","decided_at":"2026-12-01T18:32:00+00:00","status":"unknown"}
      ],
      "memory_used": [{"id":"0123456789abcdef0123456789abcdef","text":"Keep it brief.","scope":"global","source":"owner"}],
      "browser_frames": [
        {"frame_id":"abcdef0123456789abcdef0123456789","url":"https://example.com/schedule","title":"Schedule","captured_at":"2026-12-01T18:27:00+00:00"}
      ]
    }
    """

    @Test
    func conversationSnapshotRestoresWorkAndCitations() throws {
        var turn = try #require(JSONSerialization.jsonObject(with: Data(payload.utf8)) as? [String: Any])
        turn["role"] = "assistant"
        turn["text"] = "The schedule is ready. [S1]"
        turn["sources"] = turn.removeValue(forKey: "web_sources")
        turn["citation_sources"] = ["S1": ["session_id": "previous", "seq": 3, "label": "Nov 30"]]

        let companion = Companion()
        companion.apply(snapshot: ["transcript": [turn]])
        let work = try #require(companion.lastWork)
        let provenance = work.provenance
        #expect(work.steps.isEmpty)
        #expect(work.seconds == nil)
        #expect(provenance.webSources.map(\.status) == [.read, .found])
        #expect(provenance.webSources.first?.excerpt == "Friday")
        #expect(provenance.webSources.last?.link?.absoluteString == "https://example.org/agenda")
        #expect(provenance.approvalReceipts.map(\.id) == ["a1", "a2", "a3"])
        #expect(provenance.approvalReceipts.map(\.status) == [.confirmed, .denied, .unknown])
        #expect(provenance.approvalReceipts.first?.summary.hasPrefix("Approved ") == true)
        #expect(provenance.approvalReceipts.last?.summary == "Outcome unknown")
        #expect(provenance.memoryUsed.first?.scope == "global")
        #expect(provenance.memoryUsed.first?.source == "owner")
        #expect(provenance.browserFrames.first?.id == "abcdef0123456789abcdef0123456789")
        #expect(provenance.browserFrames.first?.capturedAt == "2026-12-01T18:27:00+00:00")
        #expect(companion.replySources["S1"]?.kind == .turn(session: "previous", seq: 3))

        turn["seq"] = 4
        companion.apply(snapshot: ["transcript": [turn, ["role": "user", "text": "Thanks", "seq": 5]]])
        #expect(companion.turns.first?.work == work)
        #expect(companion.lastWork == nil)
    }

    @Test
    func completionKeepsProvenanceWhenTheReplyMovesIntoHistory() throws {
        let companion = Companion()
        try handle(#"{"type":"run.started","seq":1,"data":{"run_id":"r1"}}"#, with: companion)
        var data = try #require(JSONSerialization.jsonObject(with: Data(payload.utf8)) as? [String: Any])
        data["run_id"] = "r1"
        data["messages"] = [["kind": "response", "parts": [["part_kind": "text", "content": "Checked Friday."]]]]
        let event = try JSONSerialization.data(withJSONObject: ["type": "run.completed", "seq": 2, "data": data])
        try handle(String(decoding: event, as: UTF8.self), with: companion)

        let work = try #require(companion.lastWork)
        #expect(work.steps.isEmpty)
        #expect(work.seconds != nil)
        #expect(work.provenance == TurnProvenance(data))
        #expect(companion.reply == "Checked Friday.")

        try handle(#"{"type":"user.message","seq":3,"data":{"text":"Thanks bud"}}"#, with: companion)
        #expect(companion.turns.first?.work == work)
        #expect(companion.lastWork == nil)
    }

    @Test
    func oldTurnsAndNullFieldsDoNotCreateWork() {
        #expect(Work.recorded([:]) == nil)
        #expect(Work.recorded(["web_sources": NSNull(), "sources": ["S1": ["seq": 1]]]) == nil)
    }

    @Test
    func routineCompletionKeepsWorkOnItsOwnCard() throws {
        let companion = Companion()
        var data = try #require(JSONSerialization.jsonObject(with: Data(payload.utf8)) as? [String: Any])
        data["routine_id"] = "morning"
        data["title"] = "Morning inbox"
        data["text"] = "Inbox checked."
        let event = try JSONSerialization.data(withJSONObject: ["type": "routine.completed", "seq": 2, "data": data])
        try handle(String(decoding: event, as: UTF8.self), with: companion)

        #expect(companion.turns.first?.routine?.id == "morning")
        #expect(companion.turns.first?.work?.provenance == TurnProvenance(data))
    }

    @Test(arguments: [200, 404, 500])
    func forgetPreferenceTracksServerResult(status: Int) async throws {
        let id = "0123456789abcdef0123456789abcdef"
        let companion = Companion()
        companion.connectForTesting(BudConnection(base: URL(string: "https://bud.example")!, token: "token") { request in
            #expect(request.httpMethod == "POST")
            #expect(request.url?.path == "/preferences/\(id)/forget")
            return (Data(#"{"ok":true,"id":"0123456789abcdef0123456789abcdef"}"#.utf8),
                    HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        })
        await companion.forgetPreference(id)
        #expect(companion.forgottenPreferences.contains(id) == (status != 500))
        #expect(companion.forgettingPreferences.isEmpty)
    }

    private func handle(_ json: String, with companion: Companion) throws {
        companion.handle(try #require(BudEvent(json: json[...])))
    }
}
