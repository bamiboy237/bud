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
}
