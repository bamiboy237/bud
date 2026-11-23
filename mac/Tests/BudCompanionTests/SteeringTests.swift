import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct SteeringTests {
    @Test
    func replyTextTakesOnlyTrailingSegmentAfterSteer() {
        let data: [String: Any] = [
            "messages": [
                [
                    "kind": "response",
                    "parts": [["part_kind": "text", "content": "Pre-steer partial."]],
                ],
                [
                    "kind": "request",
                    "metadata": ["bud_steer": true],
                    "parts": [["part_kind": "user-prompt", "content": "Change plan"]],
                ],
                [
                    "kind": "response",
                    "parts": [["part_kind": "text", "content": "Post-steer final."]],
                ],
            ]
        ]
        #expect(Companion.replyText(data) == "Post-steer final.")
    }

    @Test
    func midRunQueuedAndSteeredEventsArchivePreSteerReplyCleanly() throws {
        let companion = Companion()
        companion.apply(snapshot: ["last_seq": 1])
        companion.handle(try #require(BudEvent(json:
            #"{"seq":2,"type":"user.message","data":{"message_id":"m1","text":"Write poem"}}"#)))
        companion.handle(try #require(BudEvent(json:
            #"{"seq":3,"type":"run.started","data":{"run_id":"r1"}}"#)))
        companion.handle(try #require(BudEvent(json:
            #"{"seq":3,"live":true,"type":"text.delta","data":{"run_id":"r1","block":"r1:0:0","text":"Roses are red"}}"#)))

        companion.handle(try #require(BudEvent(json:
            #"{"seq":4,"type":"message.queued","data":{"message_id":"m2","text":"Make it haiku"}}"#)))
        #expect(companion.pendingMessages == [PendingMessage(id: "m2", text: "Make it haiku")])

        companion.handle(try #require(BudEvent(json:
            #"{"seq":5,"type":"user.message","data":{"message_id":"m2","text":"Make it haiku","run_id":"r1","steered":true}}"#)))
        #expect(companion.pendingMessages.isEmpty)
        #expect(companion.running)
        #expect(companion.turns.map(\.text) == ["Write poem", "Roses are red", "Make it haiku"])
        #expect(companion.reply.isEmpty)

        companion.handle(try #require(BudEvent(json:
            #"{"seq":6,"type":"run.completed","data":{"run_id":"r1","messages":[{"kind":"response","parts":[{"part_kind":"text","content":"Roses are red"}]},{"kind":"request","metadata":{"bud_steer":true},"parts":[{"part_kind":"user-prompt","content":"Make it haiku"}]},{"kind":"response","parts":[{"part_kind":"text","content":"Silent pond awaits"}]}]}}"#)))
        #expect(companion.reply == "Silent pond awaits")
        #expect(companion.turns.map(\.text) == ["Write poem", "Roses are red", "Make it haiku"])
    }
}
