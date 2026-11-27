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
            ids.map { ["approval_id": $0, "tool": "send_email", "request_id": "send-batch"] }
        }
        companion.apply(snapshot: ["last_seq": 1, "pending_approvals": pending(["a", "b"])])
        let first = try #require(companion.approvals.first)
        let shown = first.batch(in: companion.approvals)
        companion.apply(snapshot: ["last_seq": 2, "pending_approvals": pending(["a", "b", "late"])])

        companion.approveBatch(shown)

        var posts: [URLRequest] = []
        for await request in requests where request.httpMethod == "POST" { posts.append(request) }
        #expect(posts.compactMap { $0.url?.path } == ["/sessions/s1/approvals/a", "/sessions/s1/approvals/b"])
        #expect(companion.approvals.map(\.id) == ["late"])
    }
}
