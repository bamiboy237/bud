import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct RunFailureTests {
    @Test
    func liveRunFailedReusesStructuredReasonKindAndNextAndClearsPartialReply() throws {
        let companion = Companion()
        companion.apply(snapshot: ["last_seq": 1])
        companion.handle(try #require(BudEvent(json:
            #"{"seq":2,"type":"user.message","data":{"message_id":"m1","text":"Create a daily routine"}}"#)))
        companion.handle(try #require(BudEvent(json:
            #"{"seq":3,"type":"run.started","data":{"run_id":"r1"}}"#)))
        companion.handle(try #require(BudEvent(json:
            #"{"seq":3,"live":true,"type":"text.delta","data":{"run_id":"r1","block":"r1:0:0","text":"Partial output"}}"#)))

        let next = "Resend the request. If it fails again, it is a bug in bud: note the time so it can be traced."
        companion.handle(try #require(BudEvent(json:
            #"{"seq":4,"type":"run.failed","data":{"run_id":"r1","reason":"UserError","kind":"unknown","next":"Resend the request. If it fails again, it is a bug in bud: note the time so it can be traced."}}"#)))

        #expect(companion.status == .failed)
        #expect(!companion.running)
        #expect(companion.bubbleOpen)
        #expect(companion.reply.isEmpty)
        #expect(companion.shown.isEmpty)
        #expect(companion.runFailure == RunFailure(
            reason: "UserError",
            kind: "unknown",
            next: next
        ))
        #expect(companion.runFailure?.title == "Couldn’t finish this request")
        #expect(companion.runFailure?.nextStep == next)
        #expect(companion.runFailure?.detail == "UserError")

        // Sending a new message clears the failure state cleanly.
        companion.handle(try #require(BudEvent(json:
            #"{"seq":5,"type":"user.message","data":{"message_id":"m2","text":"Try again"}}"#)))
        #expect(companion.runFailure == nil)
        #expect(companion.status == .working)
    }

    @Test
    func liveRunFailedAfterConfirmedReplyKeepsConfirmedTurnInTranscript() throws {
        let companion = Companion()
        companion.apply(snapshot: [
            "last_seq": 2,
            "status": "awaiting_approval",
            "transcript": [
                ["role": "user", "text": "Send the update", "seq": 1],
                ["role": "assistant", "text": "Ready once you approve.", "seq": 2],
            ],
        ])
        #expect(companion.reply == "Ready once you approve.")
        #expect(companion.turns.map(\.text) == ["Send the update"])

        companion.handle(try #require(BudEvent(json:
            #"{"seq":3,"type":"run.started","data":{"run_id":"r2","trigger":"approval"}}"#)))
        companion.handle(try #require(BudEvent(json:
            #"{"seq":4,"type":"run.failed","data":{"run_id":"r2","reason":"ModelHTTPError: HTTP 429","kind":"rate_limit","next":"The provider is rate-limiting; resend in a minute."}}"#)))

        #expect(companion.turns.map(\.text) == ["Send the update", "Ready once you approve."])
        #expect(companion.reply.isEmpty)
        #expect(companion.status == .failed)
        #expect(companion.runFailure?.title == "Rate limit reached")
        #expect(companion.runFailure?.nextStep == "The provider is rate-limiting; resend in a minute.")
        #expect(companion.runFailure?.detail == "ModelHTTPError: HTTP 429")
    }

    @Test
    func reopenedSnapshotWithAutoPausedQueueRestoresFailedRunAndNextStep() {
        let companion = Companion()
        let next = "Resend the request. If it fails again, it is a bug in bud: note the time so it can be traced."
        companion.apply(snapshot: [
            "last_seq": 5,
            "status": "paused",
            "queue_paused": true,
            "transcript": [
                ["role": "user", "text": "Earlier question", "seq": 1],
                ["role": "assistant", "text": "Earlier answer", "seq": 2],
                ["role": "user", "text": "Create a routine", "seq": 3],
            ],
            "recent_runs": [
                ["run_id": "r1", "status": "completed"],
                [
                    "run_id": "r2",
                    "status": "failed",
                    "reason": "UserError",
                    "kind": "unknown",
                    "next": next,
                ],
            ],
        ])

        #expect(companion.status == .failed)
        #expect(!companion.running)
        #expect(companion.turns.map(\.text) == ["Earlier question", "Earlier answer", "Create a routine"])
        #expect(companion.reply.isEmpty)
        #expect(companion.runFailure?.title == "Couldn’t finish this request")
        #expect(companion.runFailure?.nextStep == next)
        #expect(companion.runFailure?.detail == "UserError")
    }

    @Test
    func reopenedSnapshotWithExplicitFailedStatusKeepsPriorAssistantTurnInTranscript() {
        let companion = Companion()
        companion.apply(snapshot: [
            "last_seq": 4,
            "status": "failed",
            "queue_paused": false,
            "transcript": [
                ["role": "user", "text": "Run the report", "seq": 1],
                ["role": "assistant", "text": "Waiting on approval.", "seq": 2],
            ],
            "recent_runs": [
                [
                    "run_id": "r2",
                    "status": "failed",
                    "reason": "Code or configuration changed; recovery stopped before tools.",
                    "kind": "recovery",
                    "next": "bud restarted with different code or settings; resend the request.",
                ],
            ],
        ])

        #expect(companion.status == .failed)
        #expect(companion.turns.map(\.text) == ["Run the report", "Waiting on approval."])
        #expect(companion.reply.isEmpty)
        #expect(companion.runFailure?.title == "Run stopped")
        #expect(companion.runFailure?.nextStep == "bud restarted with different code or settings; resend the request.")
        #expect(companion.runFailure?.detail == "Code or configuration changed; recovery stopped before tools.")
    }

    @Test
    func reopenedSnapshotPausedAfterCompletedRunDoesNotShowFailure() {
        let companion = Companion()
        companion.apply(snapshot: [
            "last_seq": 3,
            "status": "paused",
            "queue_paused": true,
            "transcript": [
                ["role": "user", "text": "Hello", "seq": 1],
                ["role": "assistant", "text": "Hi there.", "seq": 2],
            ],
            "recent_runs": [
                ["run_id": "r1", "status": "completed"],
            ],
        ])

        #expect(companion.status == .idle)
        #expect(companion.runFailure == nil)
        #expect(companion.reply == "Hi there.")
        #expect(companion.turns.map(\.text) == ["Hello"])
    }

    @Test
    func permissionFailurePointsToModelSettings() {
        let failure = RunFailure([
            "kind": "permission",
            "reason": "Provider rejected the request with private account details",
            "next": "Pick a model this key can use (/model), then resend.",
        ])

        #expect(failure.title == "Model access denied")
        #expect(failure.nextStep.contains("Settings → Models"))
        #expect(!failure.nextStep.contains("/model"))
        #expect(failure.detail == nil)
    }

    @Test
    func unavailableModelFailurePointsToModelSettings() {
        let failure = RunFailure([
            "kind": "model_unavailable",
            "next": "Configure that model's key, or pick another model (/model).",
        ])

        #expect(failure.title == "Model unavailable")
        #expect(failure.nextStep.contains("Settings → Models"))
        #expect(!failure.nextStep.contains("/model"))
    }

    @Test
    func terminalModelGuidanceWithUnknownKindPointsToModelSettings() {
        let failure = RunFailure(["next": "Try another model with /model."])

        #expect(failure.nextStep.contains("Settings → Models"))
        #expect(!failure.nextStep.contains("/model"))
    }

    @Test
    func runFailureHandlesMissingNextAndEmptyFieldsSafely() {
        let legacy = RunFailure(["reason": "interrupted before durable execution was enabled"])
        #expect(legacy.title == "Couldn’t finish this request")
        #expect(legacy.nextStep == "Resend the request. If it fails again, note the time so it can be traced.")
        #expect(legacy.detail == "interrupted before durable execution was enabled")

        let empty = RunFailure([:])
        #expect(empty.title == "Couldn’t finish this request")
        #expect(!empty.nextStep.isEmpty)
        #expect(empty.detail == nil)

        let deduplicated = RunFailure(
            reason: "Sign in to the model provider again, then resend.",
            kind: "auth",
            next: "Sign in to the model provider again, then resend."
        )
        #expect(deduplicated.title == "Sign-in failed")
        #expect(deduplicated.detail == nil)
    }
}
