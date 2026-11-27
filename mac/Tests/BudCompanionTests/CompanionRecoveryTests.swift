import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct CompanionRecoveryTests {
    @Test
    func mixedDropFillsAvailableSlotsAndSummarizesSkippedFiles() async throws {
        let companion = Companion()
        companion.draft = "Keep this draft"
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }

        let installer = directory.appending(path: "Installer.app")
        let documents = try (0...DraftAttachment.maxCount).map { index in
            let url = directory.appending(path: "Notes \(index).txt")
            try Data("Notes".utf8).write(to: url)
            return url
        }
        companion.addAttachments([installer] + documents)
        let deadline = ContinuousClock.now + .seconds(2)
        while companion.readingAttachments, ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(10))
        }

        #expect(companion.draftAttachments.map(\.reference.name) ==
                documents.dropLast().map(\.lastPathComponent))
        #expect(companion.draft == "Keep this draft")
        let skipped = try #require(documents.last?.lastPathComponent)
        #expect(companion.attachmentDropSummary == "Skipped Installer.app (unsupported), \(skipped) (attachment limit).")

        companion.clearAttachmentDropSummary()
        #expect(companion.attachmentDropSummary == nil)
    }

    @Test
    func failedTurnRestoresSentTextAndAttachmentDataAndRequestsFocus() async throws {
        let companion = Companion()
        let transport = MessageTransport()
        companion.connectForTesting(BudConnection(base: URL(string: "https://bud.example")!, token: "") {
            try await transport.respond(to: $0)
        })
        let file = DraftAttachment(reference: ChatAttachment(id: "report", name: "Report.txt"), data: Data("Report".utf8))
        companion.draft = "Send the report"
        companion.draftAttachments = [file]
        companion.send()
        await companion.sendTask?.value
        let messageIDs = await transport.messageIDs
        let messageID = try #require(messageIDs.first)

        try deliver([
            #"{"seq":1,"type":"user.message","data":{"message_id":"\#(messageID)","text":"Send the report","attachments":[{"id":"report","name":"Report.txt"}]}}"#,
            #"{"seq":2,"type":"run.started","data":{"run_id":"r1"}}"#,
            #"{"seq":3,"type":"tool.call","data":{"run_id":"r1","call_id":"c1","tool":"send_email","args":{"to":"Priya"}}}"#,
            #"{"seq":4,"type":"tool.result","data":{"run_id":"r1","call_id":"c1","outcome":"success"}}"#,
            #"{"seq":5,"type":"tool.call","data":{"run_id":"r1","call_id":"c2","tool":"workspace_read","args":{}}}"#,
            #"{"seq":6,"type":"tool.result","data":{"run_id":"r1","call_id":"c2","outcome":"success"}}"#,
            #"{"seq":7,"type":"run.failed","data":{"run_id":"r1","reason":"Connection lost"}}"#,
        ], to: companion)

        #expect(companion.canEditAndResendFailedTurn)
        #expect(companion.failedTurnExternalSteps == ["Getting the email to Priya ready"])
        let focus = companion.focusComposerNonce
        companion.editAndResendFailedTurn()
        #expect(companion.draft == "Send the report")
        #expect(companion.draftAttachments == [file])
        #expect(companion.runFailure == nil)
        #expect(!companion.canEditAndResendFailedTurn)
        #expect(companion.focusComposerNonce == focus + 1)
        let finalMessageIDs = await transport.messageIDs
        #expect(finalMessageIDs == [messageID])
    }

    @Test(arguments: [false, true])
    func stopWaitsForTheCurrentRunToEndAfterServerAcknowledgment(allWork: Bool) async throws {
        let companion = Companion()
        let endpoint = allWork ? "stop" : "stop-response"
        companion.connectForTesting(BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            if request.httpMethod == "GET" { return Self.response(request, 200, "{}") }
            #expect(request.httpMethod == "POST")
            #expect(request.url?.path == "/sessions/s1/\(endpoint)")
            return Self.response(request, 202, #"{"stop_requested":true}"#)
        })
        try deliver([#"{"seq":1,"type":"run.started","data":{"run_id":"r1"}}"#], to: companion)

        if allWork { companion.stopAllWork() } else { companion.stop() }
        #expect(companion.stopping)
        await companion.stopTask?.value
        #expect(companion.stopping)
        #expect(companion.stopError == nil)

        try deliver([#"{"seq":2,"type":"run.cancelled","data":{"run_id":"r1"}}"#], to: companion)
        #expect(!companion.stopping)
        #expect(!companion.running)
        #expect(companion.stopError == nil)
    }

    @Test
    func stopFailureReportsTheServerErrorAndLeavesTheRunActive() async throws {
        let companion = Companion()
        companion.connectForTesting(BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            Self.response(request, 503, #"{"detail":"Temporarily unavailable"}"#)
        })
        try deliver([#"{"seq":1,"type":"run.started","data":{"run_id":"r1"}}"#], to: companion)

        companion.stopResponse()
        #expect(companion.stopping)
        await companion.stopTask?.value
        #expect(!companion.stopping)
        #expect(companion.running)
        #expect(companion.stopError?.contains("Temporarily unavailable") == true)
    }

    @Test
    func responseStopDoesNotFallBackToStoppingAllWork() async {
        let companion = Companion()
        companion.connectForTesting(BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            #expect(request.url?.path == "/sessions/s1/stop-response")
            return Self.response(request, 404, #"{"detail":"Not Found"}"#)
        })

        companion.stopResponse()
        await companion.stopTask?.value
        #expect(!companion.stopping)
        #expect(companion.stopError == "This server can only stop all work.")
    }

    private func deliver(_ events: [String], to companion: Companion) throws {
        for json in events {
            companion.handle(try #require(BudEvent(json: json[...])))
        }
    }

    private nonisolated static func response(
        _ request: URLRequest, _ status: Int, _ body: String
    ) -> (Data, URLResponse) {
        (Data(body.utf8), HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }

    private actor MessageTransport {
        private(set) var messageIDs: [String] = []

        func respond(to request: URLRequest) throws -> (Data, URLResponse) {
            if request.httpMethod == "POST" {
                let body = try #require(request.httpBody)
                let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
                messageIDs.append(try #require(json?["message_id"] as? String))
            }
            return CompanionRecoveryTests.response(request, 200, "{}")
        }
    }
}
