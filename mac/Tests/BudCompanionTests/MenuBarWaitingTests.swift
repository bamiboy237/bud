import AppKit
import Testing
@testable import BudCompanion

@MainActor
struct MenuBarWaitingTests {
    @Test
    func countsWaitingCardsAndUpdatesMenuLabels() {
        let companion = Companion()
        let approval: [String: Any] = ["approval_id": "a", "tool": "send_email", "args": [:]]
        let question: [String: Any] = [
            "approval_id": "q", "tool": "ask_user", "args": ["question": "Which one?"],
        ]
        let setup = ["capability": "github", "label": "GitHub", "kind": "url", "status": "waiting"]

        companion.apply(snapshot: [
            "last_seq": 1, "pending_approvals": [approval, question], "pending_setup": [setup],
            "queued_messages": [["message_id": "queued", "text": "Run later"]],
        ])
        var waiting = MenuBarWaiting(companion: companion)
        #expect(waiting.waitingCount == 3)
        #expect(waiting.accessibilityLabel == "Bud, 3 items waiting")
        #expect(waiting.reviewTitle == "Review waiting items (3)")

        companion.apply(snapshot: ["last_seq": 2, "pending_approvals": [question]])
        waiting = MenuBarWaiting(companion: companion)
        #expect(waiting.waitingCount == 1)
        #expect(waiting.accessibilityLabel == "Bud, 1 item waiting")
        #expect(waiting.reviewTitle == "Review waiting items")

        companion.apply(snapshot: ["last_seq": 3])
        waiting = MenuBarWaiting(companion: companion)
        #expect(waiting.waitingCount == 0)
        #expect(waiting.accessibilityLabel == "Bud")
        #expect(waiting.reviewTitle == nil)
    }

    @Test
    func addsATemplateBadgeWithTheWaitingLabel() throws {
        let companion = Companion()
        let paw = try #require(NSImage(systemSymbolName: "pawprint", accessibilityDescription: nil))
        let idle = MenuBarWaiting(companion: companion).menuIcon(paw)
        #expect(idle === paw)
        #expect(idle.accessibilityDescription == "Bud")

        companion.receiveSetup([
            "capability": "github", "label": "GitHub", "kind": "url", "status": "waiting",
        ])
        let badged = MenuBarWaiting(companion: companion).menuIcon(paw)
        #expect(badged.isTemplate)
        #expect(badged.size.height == paw.size.height)
        #expect(badged.size.width > paw.size.width)
        #expect(badged.accessibilityDescription == "Bud, 1 item waiting")
    }
}
