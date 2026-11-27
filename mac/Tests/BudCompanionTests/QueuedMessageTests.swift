import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct QueuedMessageTests {
    @Test
    func editSendsTrimmedTextToTheQueuedItem() async throws {
        let companion = seededCompanion { request in
            #expect(request.httpMethod == "PATCH")
            #expect(request.url?.path == "/sessions/chat/queue/first")
            let body = try #require(request.httpBody)
            let json = try JSONSerialization.jsonObject(with: body) as? [String: String]
            #expect(json == ["text": "Updated text"])
            return Self.response(request, 200, #"{"edited":true}"#)
        }

        #expect(await companion.editQueuedMessage(id: "first", text: "  Updated text\n"))
        #expect(companion.pendingMessages.map(\.text) == ["Updated text", "Keep this"])
    }

    @Test
    func editConflictRemovesAStaleItemAndShowsNotice() async {
        let companion = seededCompanion { request in
            Self.response(request, 409, #"{"detail":"Message is no longer queued"}"#)
        }

        #expect(await companion.editQueuedMessage(id: "first", text: "Updated text") == false)
        #expect(companion.pendingMessages.map(\.id) == ["second"])
        #expect(companion.notices.last?.text == "That message already started running.")
    }

    @Test
    func editTransportFailureKeepsTheLastKnownQueue() async {
        let companion = seededCompanion { _ in throw URLError(.networkConnectionLost) }

        #expect(await companion.editQueuedMessage(id: "first", text: "Updated text") == false)
        #expect(companion.pendingMessages.map(\.text) == ["Original text", "Keep this"])
        #expect(companion.notices.last?.text == "That message already started running.")
    }

    @Test
    func aSuccessfulEditDoesNotRestoreAnItemDispatchedDuringTheRequest() async throws {
        let companion = Companion()
        let dispatch = try #require(BudEvent(json:
            #"{"seq":2,"type":"message.dispatched","data":{"message_id":"first"}}"#))
        companion.apply(snapshot: ["last_seq": 1, "queued_messages": [
            ["message_id": "first", "text": "Original text"],
        ]])
        companion.connectForTesting(BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            await companion.handle(dispatch)
            return Self.response(request, 200, #"{"edited":true}"#)
        })

        #expect(await companion.editQueuedMessage(id: "first", text: "Updated text"))
        #expect(companion.pendingMessages.isEmpty)
    }

    @Test(arguments: [true, false])
    func cancelUsesTheServerResultAndClearsTheStaleRow(cancelled: Bool) async {
        let companion = seededCompanion { request in
            #expect(request.httpMethod == "DELETE")
            #expect(request.url?.path == "/sessions/chat/queue/first")
            #expect(request.httpBody == nil)
            return Self.response(request, 200, "{\"cancelled\":\(cancelled)}")
        }

        #expect(await companion.cancelQueuedMessage(id: "first") == cancelled)
        #expect(companion.pendingMessages.map(\.id) == ["second"])
    }

    @Test
    func cancelTransportFailureKeepsTheLastKnownQueue() async {
        let companion = seededCompanion { _ in throw URLError(.networkConnectionLost) }

        #expect(await companion.cancelQueuedMessage(id: "first") == false)
        #expect(companion.pendingMessages.map(\.id) == ["first", "second"])
    }

    private func seededCompanion(transport: @escaping BudConnection.Transport) -> Companion {
        let companion = Companion()
        companion.connectForTesting(
            BudConnection(base: URL(string: "https://bud.example")!, token: "", transport: transport),
            session: "chat")
        companion.apply(snapshot: ["queued_messages": [
            ["message_id": "first", "text": "Original text"],
            ["message_id": "second", "text": "Keep this"],
        ]])
        return companion
    }

    private nonisolated static func response(
        _ request: URLRequest, _ status: Int, _ body: String
    ) -> (Data, URLResponse) {
        (Data(body.utf8), HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }
}
