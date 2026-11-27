import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct DesktopDeliveryInspectorTests {
    @Test(arguments: [DesktopDeliveryAction.approve, .cancel, .retarget])
    func keepsDeliveryDetailsDuringAnActionAndAfterItFails(_ action: DesktopDeliveryAction) async throws {
        let state = DesktopDeliveryState()
        let ready = try payload(phase: "ready")
        let failure = Data(#"{"detail":"Please try again."}"#.utf8)
        let selection = selection { request in
            if request.httpMethod == "POST" {
                #expect(request.url?.path.hasSuffix("/browser/\(action.rawValue)") == true)
                await MainActor.run {
                    #expect(state.action == action)
                    Self.expectDetails(state)
                }
                return try Self.response(to: request, with: failure, status: 409)
            }
            return try Self.response(to: request, with: ready)
        }

        await state.refresh(selection)
        await state.perform(action, on: selection, desktopID: action == .cancel ? nil : "mac")

        #expect(state.action == nil)
        Self.expectDetails(state)
        #expect(state.problem == "bud answered 409: Please try again.")

        await state.refresh(selection)
        Self.expectDetails(state)
        #expect(state.problem == "bud answered 409: Please try again.")
    }

    @Test
    func keepsTheCurrentDesktopUntilTheRetargetRefreshFinishes() async throws {
        let state = DesktopDeliveryState()
        let ready = try payload(phase: "ready")
        let retargeted = try payload(phase: "queued", desktop: "Studio", desktopID: "studio")
        let selection = selection { request in
            if request.httpMethod == "POST" {
                return try Self.response(to: request, with: Data(#"{"state":"queued"}"#.utf8))
            }
            let isRetargeting = await MainActor.run {
                guard state.action == .retarget else { return false }
                Self.expectDetails(state)
                return true
            }
            return try Self.response(to: request, with: isRetargeting ? retargeted : ready)
        }

        await state.refresh(selection)
        await state.perform(.retarget, on: selection, desktopID: "studio")

        #expect(state.action == nil)
        #expect(state.status?.desktop == "Studio")
        #expect(state.status?.desktopID == "studio")
        #expect(state.problem == nil)
    }

    @Test(arguments: ["uncertain", "sending"])
    func checksForReceiptAfterAnUnconfirmedStatusOrRefreshTimeout(_ phase: String) async throws {
        let state = DesktopDeliveryState()
        let waiting = try payload(phase: phase)
        let delivered = try payload(phase: "delivered")
        let selection = selection { request in
            if request.httpMethod == "POST" {
                #expect(phase == "uncertain")
                #expect(request.url?.path.hasSuffix("/browser/verify") == true)
                return try Self.response(to: request, with: Data(#"{"state":"sending"}"#.utf8))
            }
            let checking = await state.action == .verify
            if checking { return try Self.response(to: request, with: delivered) }
            if phase == "sending", await state.status != nil { throw URLError(.timedOut) }
            return try Self.response(to: request, with: waiting)
        }

        await state.refresh(selection)
        #expect(state.canCheckReceipt == (phase == "uncertain"))
        if phase == "sending" {
            await state.refresh(selection)
            #expect(state.problem != nil)
        }
        Self.expectDetails(state)
        #expect(state.canCheckReceipt)

        await state.perform(.verify, on: selection)
        #expect(state.status?.phase == "delivered")
        #expect(!state.canCheckReceipt)
        #expect(state.problem == nil)
    }

    private static func expectDetails(_ state: DesktopDeliveryState) {
        #expect(state.status?.file.name == "invoice.pdf")
        #expect(state.status?.url == "https://example.test/send")
        #expect(state.status?.desktop == "Mac")
        #expect(state.status?.desktopID == "mac")
    }

    private func payload(phase: String, desktop: String = "Mac", desktopID: String = "mac") throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "file": ["id": "file", "name": "invoice.pdf"],
            "browser": ["url": "https://example.test/send"],
            "steps": [[
                "id": "reference", "desktop": desktop, "desktop_id": desktopID,
                "browser_state": phase, "revoked": false,
            ]],
        ])
    }

    private func selection(transport: @escaping BudConnection.Transport) -> DesktopDeliverySelection {
        DesktopDeliverySelection(
            connection: BudConnection(base: URL(string: "https://bud.example")!, token: "", transport: transport),
            session: "chat", jobID: "delivery")
    }

    private nonisolated static func response(
        to request: URLRequest, with data: Data, status: Int = 200
    ) throws -> (Data, URLResponse) {
        let url = try #require(request.url)
        let response = try #require(HTTPURLResponse(
            url: url, statusCode: status, httpVersion: nil, headerFields: nil))
        return (data, response)
    }
}
