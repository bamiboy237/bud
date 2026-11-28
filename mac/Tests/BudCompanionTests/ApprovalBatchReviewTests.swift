import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct ApprovalBatchReviewTests {
    @Test(.timeLimit(.minutes(1)))
    func onlyReviewedActionsAreSentAfterAnotherActionArrives() async throws {
        let companion = Companion()
        let (requests, continuation) = AsyncStream.makeStream(of: URLRequest.self)
        companion.connectForTesting(BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            continuation.yield(request)
            let applying = request.httpMethod == "POST"
            if !applying { continuation.finish() }
            return (
                applying ? Data(#"{"applied":true}"#.utf8) : Data(),
                HTTPURLResponse(
                    url: request.url!, statusCode: applying ? 200 : 503,
                    httpVersion: nil, headerFields: nil)!
            )
        })
        func pending(_ ids: [String]) -> [[String: Any]] {
            ids.map { ["approval_id": $0, "tool": "archive_email", "request_id": "inbox-batch"] }
        }
        companion.apply(snapshot: ["last_seq": 1, "pending_approvals": pending(["a", "b"])])
        let first = try #require(companion.approvals.first)
        let shown = first.batch(in: companion.approvals)
        companion.apply(snapshot: ["last_seq": 2, "pending_approvals": pending(["a", "b", "late"])])

        let external = try #require(Approval([
            "approval_id": "external", "tool": "publish_update", "request_id": "inbox-batch",
            "review": ["require_full_preview": true],
        ], jobs: [:]))
        companion.approveBatch(shown + [external])

        var posts: [URLRequest] = []
        for await request in requests where request.httpMethod == "POST" { posts.append(request) }
        #expect(posts.compactMap { $0.url?.path } == ["/sessions/s1/approvals/a", "/sessions/s1/approvals/b"])
        #expect(companion.approvals.map(\.id) == ["late"])
    }

    @Test
    func fullPreviewsStayIndividual() throws {
        let body = "Hi Dana,\n\n" + String(repeating: "Full line of text.\n", count: 30) + "Last line."
        let metadataSend = try #require(Approval([
            "approval_id": "send", "tool": "publish_update", "request_id": "inbox",
            "args": ["to": ["dana@example.com"], "subject": "Schedule", "body": body],
            "review": ["require_full_preview": true], "grantable": true,
        ], jobs: [:]))
        let legacySend = try #require(Approval([
            "approval_id": "slack", "tool": "SLACK_SEND_MESSAGE", "request_id": "inbox",
            "args": ["channel": "#team", "text": body],
        ], jobs: [:]))
        let draftOnly = try #require(Approval([
            "approval_id": "draft", "tool": "GMAIL_SEND_DRAFT", "request_id": "inbox",
            "args": ["draft_id": "draft-1"],
        ], jobs: [:]))
        let archive = try #require(Approval([
            "approval_id": "archive", "tool": "archive_email", "request_id": "inbox",
        ], jobs: [:]))
        let items = [metadataSend, legacySend, draftOnly, archive]

        #expect(archive.batch(in: items).map(\.id) == ["archive"])
        #expect(metadataSend.batch(in: items).isEmpty)
        #expect(legacySend.batch(in: items).isEmpty)
        #expect(ApprovalBatch.reviewedIDs(items, pending: items) == ["archive"])
        #expect(!metadataSend.grantable)
        #expect(metadataSend.hasFullPreview)
        #expect(metadataSend.facts.map(\.value) == ["dana@example.com", "Schedule"])
        #expect(metadataSend.body == body)
        #expect(legacySend.hasFullPreview)
        #expect(legacySend.facts.map(\.value) == ["#team"])
        #expect(legacySend.body == body)
        #expect(!draftOnly.hasFullPreview)
    }
}
