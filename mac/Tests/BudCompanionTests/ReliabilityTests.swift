import AppKit
import Foundation
import Testing
@testable import BudCompanion

struct ReliabilityTests {
    // MARK: - ConnectionMachine & ReconnectBackoff

    @Test
    func connectionMachineExponentialBackoffScheduleAndReset() {
        var machine = ConnectionMachine()
        #expect(machine.phase == .disconnected)
        #expect(machine.attempt == 0)
        #expect(machine.backoffAttempt == 0)

        let startEffects = machine.reduce(.start(resetBackoff: true))
        #expect(machine.attempt == 1)
        #expect(machine.phase == .connecting)
        #expect(startEffects == [.cancelPending, .beginHandshake(attempt: 1)])

        // Zero-jitter midpoint (0.5) yields exact 1s -> 2s -> 4s -> 8s -> 15s -> 15s cap.
        let expectedSeconds = [1.0, 2.0, 4.0, 8.0, 15.0, 15.0]
        for (idx, seconds) in expectedSeconds.enumerated() {
            let expectedDelay = Duration.milliseconds(Int(seconds * 1000))
            let failEffects = machine.reduce(
                .handshakeFailed(
                    attempt: machine.attempt,
                    reason: "unreachable",
                    retryable: true,
                    jitterUnit: 0.5
                ))
            #expect(machine.backoffAttempt == idx + 1)
            #expect(machine.phase == .reconnecting(reason: "unreachable", delay: expectedDelay))
            #expect(failEffects == [.scheduleRetry(attempt: machine.attempt, delay: expectedDelay)])

            let currentAttempt = machine.attempt
            let timerEffects = machine.reduce(.retryTimerFired(attempt: currentAttempt))
            #expect(machine.attempt == currentAttempt + 1)
            #expect(machine.phase == .connecting)
            #expect(machine.backoffAttempt == idx + 1)
            #expect(timerEffects == [.cancelPending, .beginHandshake(attempt: machine.attempt)])
        }

        // Successful stream connect resets backoff to 0 so the next drop starts at 1s again.
        let connectEffects = machine.reduce(.streamConnected(attempt: machine.attempt))
        #expect(connectEffects.isEmpty)
        #expect(machine.phase == .connected)
        #expect(machine.backoffAttempt == 0)

        _ = machine.reduce(.streamLost(attempt: machine.attempt, reason: "lost", jitterUnit: 0.5))
        #expect(machine.backoffAttempt == 1)
        #expect(machine.phase == .reconnecting(reason: "lost", delay: .seconds(1)))

        // Receiving a valid event also clears backoff and marks connected.
        _ = machine.reduce(.streamEventReceived(attempt: machine.attempt))
        #expect(machine.phase == .connected)
        #expect(machine.backoffAttempt == 0)
    }

    @Test
    func reconnectBackoffBoundedJitterStaysWithinRangeAndCap() {
        var backoff = ReconnectBackoff(baseSeconds: 1.0, maxSeconds: 15.0, jitterFraction: 0.15)
        let bases = [1.0, 2.0, 4.0, 8.0, 15.0, 15.0]
        for (index, base) in bases.enumerated() {
            let low = backoff.delay(forAttempt: index + 1, jitterUnit: 0.0)
            let high = backoff.delay(forAttempt: index + 1, jitterUnit: 1.0)
            let minBound = Duration.milliseconds(Int((max(0.5, base * 0.85) * 1000).rounded()))
            let maxBound = Duration.milliseconds(Int((min(15.0, base * 1.15) * 1000).rounded()))
            #expect(low >= minBound)
            #expect(high <= maxBound)
            #expect(high <= .seconds(15))
            #expect(low <= high)
        }
        let next = backoff.nextDelay(jitterUnit: 1.0)
        #expect(backoff.attemptCount == 1)
        #expect(next == .milliseconds(1150))
    }

    @Test
    func connectionMachineRejectsStaleAttemptCallbacks() {
        var machine = ConnectionMachine()
        _ = machine.reduce(.start(resetBackoff: true))
        let staleAttempt = machine.attempt // 1

        // A newer start bumps attempt to 2; callbacks from attempt 1 must be ignored.
        _ = machine.reduce(.start(resetBackoff: true))
        let activeAttempt = machine.attempt // 2
        #expect(activeAttempt == 2)

        let staleHandshake = machine.reduce(.handshakeSucceeded(attempt: staleAttempt))
        #expect(staleHandshake.isEmpty)
        let staleFail = machine.reduce(
            .handshakeFailed(attempt: staleAttempt, reason: "old", retryable: true, jitterUnit: 0.5)
        )
        #expect(staleFail.isEmpty)
        let staleConnect = machine.reduce(.streamConnected(attempt: staleAttempt))
        #expect(staleConnect.isEmpty)
        let staleEvent = machine.reduce(.streamEventReceived(attempt: staleAttempt))
        #expect(staleEvent.isEmpty)
        let staleLost = machine.reduce(.streamLost(attempt: staleAttempt, reason: "old", jitterUnit: 0.5))
        #expect(staleLost.isEmpty)
        let staleFatal = machine.reduce(.streamFatal(attempt: staleAttempt, reason: "old 401"))
        #expect(staleFatal.isEmpty)
        let staleTimer = machine.reduce(.retryTimerFired(attempt: staleAttempt))
        #expect(staleTimer.isEmpty)

        #expect(machine.phase == .connecting)
        #expect(machine.attempt == activeAttempt)
        #expect(machine.backoffAttempt == 0)

        // Fatal errors on active attempt stop retrying and enter .problem.
        let fatalEffects = machine.reduce(.streamFatal(attempt: activeAttempt, reason: "bad token"))
        #expect(machine.phase == .problem("bad token"))
        #expect(fatalEffects == [.cancelPending])
        let retryAfterFatal = machine.reduce(.retryTimerFired(attempt: activeAttempt))
        #expect(retryAfterFatal.isEmpty)
    }

    // MARK: - ScrollFollowMachine

    @Test
    func scrollFollowMachineThresholdsAndJumpSuppressionWindow() {
        var machine = ScrollFollowMachine()
        let t0 = Date(timeIntervalSince1970: 1_000)

        #expect(machine.atBottom)
        #expect(!machine.showsJumpButton)

        // Not meaningfully scrollable (contentHeight <= containerHeight + 80): always pinned.
        machine.reduce(
            .geometryChanged(
                .init(contentHeight: 580, containerHeight: 500, visibleMaxY: 300),
                now: t0
            ))
        #expect(machine.atBottom)
        #expect(!machine.showsJumpButton)

        // Scrollable, within 140pt of bottom: stays pinned, no jump button.
        machine.reduce(
            .geometryChanged(
                .init(contentHeight: 1000, containerHeight: 500, visibleMaxY: 860),
                now: t0.addingTimeInterval(1)
            ))
        #expect(machine.atBottom)
        #expect(!machine.showsJumpButton)

        // Scrolled up more than 140pt: reveals jump button.
        machine.reduce(
            .geometryChanged(
                .init(contentHeight: 1000, containerHeight: 500, visibleMaxY: 859),
                now: t0.addingTimeInterval(2)
            ))
        #expect(!machine.atBottom)
        #expect(machine.showsJumpButton)

        // Jump to bottom dismisses button immediately and opens 0.35s suppression guard.
        let jumpAt = t0.addingTimeInterval(3)
        let effects = machine.reduce(.jumpToBottom(now: jumpAt))
        #expect(effects == [.scrollToEnd])
        #expect(machine.atBottom)
        #expect(!machine.showsJumpButton)
        #expect(machine.isGuardActive(at: jumpAt.addingTimeInterval(0.34)))
        #expect(!machine.isGuardActive(at: jumpAt.addingTimeInterval(0.35)))

        // Intermediate scrolled-up geometry events inside 0.35s guard are suppressed.
        machine.reduce(
            .geometryChanged(
                .init(contentHeight: 1000, containerHeight: 500, visibleMaxY: 700),
                now: jumpAt.addingTimeInterval(0.20)
            ))
        #expect(machine.atBottom)
        #expect(!machine.showsJumpButton)

        // After 0.35s guard expires, user scrolling up reveals jump button again.
        machine.reduce(
            .geometryChanged(
                .init(contentHeight: 1000, containerHeight: 500, visibleMaxY: 700),
                now: jumpAt.addingTimeInterval(0.35)
            ))
        #expect(!machine.atBottom)
        #expect(machine.showsJumpButton)
    }

    // MARK: - ApprovalDeckMachine

    @Test
    func approvalDeckMachineIdempotencyBatchLockAndClamping() {
        var deck = ApprovalDeckMachine()
        deck.sync(ids: ["a", "b", "c"])
        #expect(deck.safeIndex == 0)
        #expect(deck.activeID == "a")

        // Cycle to the last card ("c", index 2).
        let next1 = deck.selectNext()
        let next2 = deck.selectNext()
        let next3 = deck.selectNext()
        #expect(next1)
        #expect(next2)
        #expect(!next3)
        #expect(deck.safeIndex == 2)
        #expect(deck.activeID == "c")

        // Single-click idempotency: second beginDecision for "c" while deciding is rejected.
        let attemptC = deck.beginDecision(id: "c")
        #expect(attemptC != nil)
        #expect(deck.deciding == ["c"])
        let duplicateAttempt = deck.beginDecision(id: "c")
        #expect(duplicateAttempt == nil)

        // Batch approval is locked while any single decision is in flight.
        let blockedBatch = deck.beginBatch(ids: ["a", "b"])
        #expect(blockedBatch == nil)

        // Stale completion attempt cannot settle "c".
        let staleCompleted = deck.completeDecision(id: "c", attempt: (attemptC ?? 0) + 99, succeeded: true)
        #expect(!staleCompleted)
        #expect(deck.ids == ["a", "b", "c"])

        // Valid completion removes "c" and clamps index from 2 to 1 ("b").
        let completedC = deck.completeDecision(id: "c", attempt: attemptC, succeeded: true)
        #expect(completedC)
        #expect(deck.ids == ["a", "b"])
        #expect(deck.safeIndex == 1)
        #expect(deck.activeID == "b")
        #expect(deck.deciding.isEmpty)

        // Arriving card keeps active selection steady at index 1 ("b").
        let appendedD = deck.append(id: "d")
        let duplicateD = deck.append(id: "d")
        #expect(appendedD)
        #expect(!duplicateD)
        #expect(deck.ids == ["a", "b", "d"])
        #expect(deck.safeIndex == 1)
        #expect(deck.activeID == "b")

        // Batch approval locks individual decisions until batch completes.
        let batchAttempt = deck.beginBatch(ids: ["a", "b"])
        #expect(batchAttempt != nil)
        #expect(deck.approvingBatch)
        #expect(deck.deciding == ["a", "b"])
        let decisionWhileBatch = deck.beginDecision(id: "d")
        let secondBatch = deck.beginBatch(ids: ["d"])
        #expect(decisionWhileBatch == nil)
        #expect(secondBatch == nil)

        // Partial batch failure preserves failed item + failure text and clears approved item.
        let batchCompleted = deck.completeBatch(
            attempt: batchAttempt,
            ids: ["a", "b"],
            approved: ["a"],
            failures: ["b": "Network timeout"]
        )
        #expect(batchCompleted)
        #expect(!deck.approvingBatch)
        #expect(deck.deciding.isEmpty)
        #expect(deck.ids == ["b", "d"])
        #expect(deck.failures == ["b": "Network timeout"])
        #expect(deck.safeIndex == 1)

        // Removing remaining items clamps index to 0 and clears failures/problems.
        deck.sync(ids: [])
        #expect(deck.safeIndex == 0)
        #expect(deck.activeID == nil)
        #expect(deck.failures.isEmpty)
        #expect(deck.decisionProblem == nil)
    }

    @Test
    func approvalDeckMachineBlocksCyclingWhileAnimating() {
        var deck = ApprovalDeckMachine()
        deck.sync(ids: ["a", "b"])
        deck.setAnimating(true)
        #expect(!deck.canSelectNext)
        let movedWhileAnimating = deck.selectNext()
        #expect(!movedWhileAnimating)
        deck.setAnimating(false)
        #expect(deck.canSelectNext)
        let movedAfter = deck.selectNext()
        #expect(movedAfter)
        #expect(deck.safeIndex == 1)
    }

    // MARK: - DiagnosticsFormatter & Copy Diagnostics

    @Test
    func diagnosticsFormatterStructuresSnapshotAndRedactsSecrets() {
        let ownerSecret = "super_secret_owner_token_abcdefghijklmnopqrstuvwxyz12345"
        let snapshot = DiagnosticsSnapshot(
            appVersion: "0.1.0",
            appBuild: "42",
            macOSVersion: "Version 15.2 (Build 24C101)",
            connectionPhase: "reconnecting",
            connectionAttempt: 4,
            backoffAttempt: 3,
            sessionID: "sess_123",
            serverURL: "https://alice:hunter2@bud.example.com:8443/api?token=\(ownerSecret)#frag",
            petStatus: "needsYou",
            running: true,
            pendingApprovalsCount: 2,
            pendingSetupsCount: 1,
            activeTasksCount: 3,
            stalledTasksCount: 1,
            decidingCount: 1,
            approvingBatch: false,
            voiceState: "ready",
            voiceKey: "⌥ Space",
            needsAccessibility: false,
            recentNotices: [
                "Started “Inbox triage”",
                "Auth header Bearer \(ownerSecret) failed",
            ],
            recentErrors: [
                "HTTP 401 OWNER_TOKEN=\(ownerSecret) sk-ant-api03-1234567890abcdef",
                "Request failed at https://bud.example.com:8443/sessions?auth=secret123",
            ]
        )

        let formatted = DiagnosticsFormatter.format(snapshot, knownSecrets: [ownerSecret])

        #expect(formatted.contains("App: 0.1.0 (42)"))
        #expect(formatted.contains("macOS: Version 15.2 (Build 24C101)"))
        #expect(formatted.contains("Connection: reconnecting (attempt #4, backoff #3)"))
        #expect(formatted.contains("Server: https://bud.example.com:8443"))
        #expect(formatted.contains("Session: sess_123"))
        #expect(formatted.contains("Pending: approvals=2, setups=1, tasks=3 (stalled=1)"))
        #expect(formatted.contains("Voice: ready (key: ⌥ Space, accessibility_needed: no)"))

        // Secrets, credentials, and URL query parameters must never appear.
        #expect(!formatted.contains(ownerSecret))
        #expect(!formatted.contains("hunter2"))
        #expect(!formatted.contains("alice:"))
        #expect(!formatted.contains("secret123"))
        #expect(!formatted.contains("sk-ant-api03-1234567890abcdef"))
        #expect(!formatted.contains("?token="))
        #expect(formatted.contains("[REDACTED]"))
    }

    @Test @MainActor
    func companionCopyDiagnosticsExcludesConversationBodiesAndTokens() {
        let companion = Companion()
        let token = "owner_token_0123456789abcdefghijklmnopqrstuvwxyzABCDEF"
        let conn = BudConnection(
            base: URL(string: "http://127.0.0.1:8420")!,
            token: token,
            scope: "scope-secret"
        )
        companion.connectForTesting(conn, session: "sess-abc")
        companion.draft = "CONFIDENTIAL_DRAFT_BODY_DO_NOT_LEAK"
        companion.apply(snapshot: [
            "last_seq": 1,
            "transcript": [
                ["role": "user", "text": "PRIVATE_USER_MESSAGE_BODY"],
                ["role": "assistant", "text": "PRIVATE_ASSISTANT_REPLY_BODY"],
            ],
            "pending_approvals": [
                ["approval_id": "ap1", "tool": "send_email", "args": ["body": "SECRET_EMAIL"]],
            ],
        ])

        companion.copyDiagnostics()

        let copied = NSPasteboard.general.string(forType: .string) ?? ""
        #expect(copied.contains("Session: sess-abc"))
        #expect(copied.contains("Server: http://127.0.0.1:8420"))
        #expect(copied.contains("Pending: approvals=1"))
        #expect(!copied.contains(token))
        #expect(!copied.contains("CONFIDENTIAL_DRAFT_BODY_DO_NOT_LEAK"))
        #expect(!copied.contains("PRIVATE_USER_MESSAGE_BODY"))
        #expect(!copied.contains("PRIVATE_ASSISTANT_REPLY_BODY"))
        #expect(!copied.contains("SECRET_EMAIL"))
        #expect(companion.diagnosticsCopied)
        #expect(companion.notices.contains { $0.text == "Diagnostics copied" })
    }
}
