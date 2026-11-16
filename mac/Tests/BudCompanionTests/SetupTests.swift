import AppKit
import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct SetupTests {
    @Test
    func waitingSetupNeedsAttentionAndOpensTheBubble() {
        let companion = Companion()
        companion.apply(snapshot: [
            "last_seq": 1,
            "pending_setup": [[
                "capability": "github", "label": "GitHub", "kind": "url", "status": "waiting",
            ]],
            "transcript": [["role": "assistant", "text": "Connect GitHub to continue."]],
        ])
        #expect(companion.status == .needsYou)
        #expect(companion.bubbleOpen)
    }

    @Test
    func snapshotRefreshKeepsTheLiveLinkAndStatusKeepsTheCardDetails() {
        let companion = Companion()
        let raw: [String: Any] = [
            "capability": "github", "label": "GitHub", "kind": "url", "status": "waiting",
            "url": "https://example.test/sign-in",
        ]
        companion.receiveSetup(raw, replacing: true)
        companion.receiveSetup(raw, replacing: true)
        #expect(companion.setups.count == 1)
        companion.apply(snapshot: [
            "last_seq": 2,
            "pending_setup": [[
                "capability": "github", "label": "GitHub", "kind": "url", "status": "waiting",
            ]],
        ])
        #expect(companion.setups.first?.url?.absoluteString == "https://example.test/sign-in")
        companion.receiveSetup([
            "capability": "github", "status": "failed", "reason": "Try again.",
        ])
        #expect(companion.setups.first?.label == "GitHub")
        #expect(companion.setups.first?.reason == "Try again.")
        #expect(companion.setups.first?.url != nil)
        companion.receiveSetup(["capability": "github", "status": "connected"])
        #expect(companion.setups.isEmpty)
        #expect(companion.status == .idle)
    }

    @Test
    func signInLinksAreHTTPSOnlyAndRemovedWithTheirCard() {
        for url in ["http://example.test", "file:///etc/passwd", "javascript:alert(1)",
                    "https://user:password@example.test"] {
            #expect(CapabilitySetup.signInURL(url) == nil)
        }
        let companion = Companion()
        companion.receiveSetup([
            "capability": "github", "url": "https://example.test/first", "status": "waiting",
        ], replacing: true)
        companion.receiveSetup([
            "capability": "github", "status": "waiting",
        ], replacing: true)
        #expect(companion.setups.first?.url == nil)
        companion.apply(snapshot: ["last_seq": 1, "pending_setup": []])
        #expect(companion.setups.isEmpty)
    }

    @Test
    func cancellationHandlesEachAppIndependently() {
        let companion = Companion()
        for app in ["slack", "notion", "gmail"] {
            companion.receiveSetup([
                "capability": app, "label": app.capitalized, "status": "waiting",
            ], replacing: true)
        }
        companion.receiveSetup(["capability": "slack", "status": "cancelled"])
        #expect(companion.setups.map(\.id) == ["notion", "gmail"])
        #expect(companion.needsAttention)
    }

    @Test
    func windowWillClosePreservesBubbleWhenSetupNeedsAttention() {
        let companion = Companion.shared
        companion.apply(snapshot: [
            "last_seq": 1,
            "pending_setup": [[
                "capability": "github", "label": "GitHub", "kind": "url", "status": "waiting",
            ]],
            "transcript": [],
        ])
        #expect(companion.needsAttention)
        companion.windowOpen = true
        companion.bubbleOpen = false
        ConversationWindow.shared.windowWillClose(Notification(name: NSWindow.willCloseNotification))
        #expect(!companion.windowOpen)
        #expect(companion.bubbleOpen)
        companion.receiveSetup(["capability": "github", "status": "connected"])
    }

    @Test
    func inlineErrorsClearOnRetryAndRemovedOnCompletion() {
        let companion = Companion()
        companion.receiveSetup([
            "capability": "github", "label": "GitHub", "kind": "url", "status": "waiting",
        ], replacing: true)
        companion.receiveSetup([
            "capability": "github", "status": "failed", "reason": "Sign-in expired.",
        ])
        #expect(companion.setups.first?.reason == "Sign-in expired.")
        // When connectSetup is called (even without connection), it clears the previous problem
        companion.connectSetup(companion.setups[0])
        #expect(companion.setupProblems["github"] == nil)

        // When setup completes or cancels, errors are cleaned up
        companion.receiveSetup(["capability": "github", "status": "connected"])
        #expect(companion.setups.isEmpty)
        #expect(companion.setupProblems["github"] == nil)
    }

    @Test
    func activationNotificationTriggersImmediateSetupRefresh() {
        let companion = Companion()
        companion.receiveSetup([
            "capability": "github", "label": "GitHub", "kind": "url", "status": "waiting",
        ], replacing: true)
        #expect(companion.needsAttention)
        NotificationCenter.default.post(name: NSApplication.didBecomeActiveNotification, object: nil)
        #expect(companion.needsAttention)
    }

    @Test
    func fastPollingTriggerSetsRemainingCount() {
        let companion = Companion()
        companion.receiveSetup([
            "capability": "github", "label": "GitHub", "kind": "url", "status": "waiting",
        ], replacing: true)
        #expect(companion.fastPollRemaining == 0)
        companion.triggerFastPolling()
        #expect(companion.fastPollRemaining == 3)
    }

    @Test
    func childPupsAreCappedAtFour() {
        let companion = Companion()
        companion.apply(snapshot: [
            "last_seq": 1,
            "tasks": [
                ["task_id": 1, "title": "Index repo", "status": "running"],
                ["task_id": 2, "title": "Check CI", "status": "running"],
                ["task_id": 3, "title": "Review diff", "status": "running"],
                ["task_id": 4, "title": "Run tests", "status": "running"],
                ["task_id": 5, "title": "Extra task", "status": "running"],
            ],
        ])
        #expect(companion.activeTasks.count == 5)
        #expect(companion.visiblePups.count == 4)
        #expect(companion.visiblePups.map(\.id) == [1, 2, 3, 4])
    }
}
