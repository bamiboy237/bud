import Foundation
import Testing
@testable import BudCompanion

struct DesktopDeliveryTests {
    @Test
    func approvalRequiresAnIdentifiedFileAndAvailableDesktop() throws {
        let first = try #require(DesktopDeliveryStatus(status(fileID: "first")))
        let second = try #require(DesktopDeliveryStatus(status(fileID: "second")))
        #expect(first.file.name == second.file.name)
        #expect(first.file.id != second.file.id)
        #expect(first.canApprove)
        #expect(DesktopDeliveryStatus(status(fileID: "")) == nil)
        #expect(DesktopDeliveryStatus(status(desktopID: "")) == nil)
        #expect(DesktopDeliveryStatus(status(phase: "queued"))?.canApprove == false)
        #expect(DesktopDeliveryStatus(status(revoked: true))?.canApprove == false)
    }

    private func status(
        fileID: String = "file", desktopID: String = "desktop",
        phase: String = "ready", revoked: Bool = false
    ) -> [String: Any] {
        [
            "file": ["id": fileID, "name": "invoice.pdf"],
            "browser": ["url": "https://example.test/send"],
            "steps": [[
                "id": "reference", "desktop": "Mac", "desktop_id": desktopID,
                "browser_state": phase, "revoked": revoked,
            ]],
        ]
    }
}
