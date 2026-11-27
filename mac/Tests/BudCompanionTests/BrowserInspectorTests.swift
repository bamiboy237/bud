import AppKit
import Testing
@testable import BudCompanion

struct BrowserInspectorTests {
    @Test
    func groupsTabsByBrowserEvenWhenNamesMatch() {
        let tabs = [
            ["id": "7", "title": "Work", "browser": "remote"],
            ["id": "6", "title": "Home", "browser": "local"],
            ["id": "8", "title": "Unlabeled"],
            ["id": "9", "title": "Notes", "browser": "remote"],
        ].compactMap(BrowserTab.init)
        let groups = BrowserTabGroup.make(tabs: tabs, browsers: [
            ["id": "local", "name": "Chrome"],
            ["id": "remote", "name": "Chrome"],
        ])

        #expect(groups.map(\.id) == ["local", "remote", nil])
        #expect(groups.map(\.name) == ["Chrome", "Chrome", "Browse"])
        #expect(groups.map { $0.tabs.map(\.id) } == [["6"], ["7", "9"], ["8"]])
    }

    @Test
    func followsBudToNewAndExistingTabs() {
        var focus = BrowserTabFocus()
        focus.refresh(tabIDs: ["example", "docs"], active: nil)
        #expect(focus.selected == "example")

        focus.refresh(tabIDs: ["example", "docs", "gemini"], active: "gemini")
        #expect(focus.selected == "gemini")

        focus.selected = "example"
        focus.refresh(tabIDs: ["example", "docs", "gemini"], active: "gemini")
        #expect(focus.selected == "example")

        focus.refresh(tabIDs: ["example", "docs", "gemini"], active: "docs")
        #expect(focus.selected == "docs")

        focus.refresh(tabIDs: ["gemini"], active: nil)
        #expect(focus.selected == "gemini")
    }

    @Test @MainActor
    func prunesClosedTabPreviewsAndIssues() {
        let state = BrowserInspectorState()
        state.applyStatus(["tabs": tabs("docs", "mail", "notes", "calendar")])
        let preview = NSImage(size: NSSize(width: 16, height: 10))
        state.recordPreview(preview, for: "docs")
        state.recordPreview(preview, for: "mail")
        state.recordPreview(nil, for: "notes")
        state.recordPreview(nil, for: "calendar")

        state.applyStatus(["tabs": tabs("docs", "notes")])

        #expect(Set(state.previews.keys) == ["docs"])
        #expect(state.previews["docs"]?.image === preview)
        #expect(Set(state.previewIssues.keys) == ["notes"])
    }

    @Test @MainActor
    func clearsAllPreviewsOnPrivateStatus() {
        let state = BrowserInspectorState()
        state.applyStatus(["tabs": tabs("docs", "mail"), "active_tab": "docs"])
        state.recordPreview(NSImage(size: NSSize(width: 16, height: 10)), for: "docs")
        state.recordPreview(nil, for: "mail")

        state.applyStatus(["private": true, "handoff_open": true])

        #expect(state.privateInput)
        #expect(state.handoffOpen)
        #expect(state.groups.isEmpty)
        #expect(state.activeTabID == nil)
        #expect(state.previews.isEmpty)
        #expect(state.previewIssues.isEmpty)
    }

    @Test @MainActor
    func tracksBudsActiveTabIndependentlyOfSelection() {
        let state = BrowserInspectorState()
        let status: [String: Any] = ["tabs": tabs("docs", "mail"), "active_tab": "mail"]
        state.applyStatus(status)
        #expect(state.activeTabID == "mail")
        #expect(state.focus.selected == "mail")

        let preview = NSImage(size: NSSize(width: 16, height: 10))
        state.recordPreview(preview, for: "mail")
        let refreshID = state.refreshID
        state.select("docs")
        #expect(state.focus.selected == "docs")
        #expect(state.refreshID != refreshID)
        #expect(state.previews["mail"]?.image === preview)
        state.applyStatus(status)
        #expect(state.focus.selected == "docs")
        #expect(state.activeTabID == "mail")

        state.applyStatus(["tabs": tabs("docs", "mail")])
        #expect(state.activeTabID == nil)
    }

    @Test(arguments: [423, 409]) @MainActor
    func dropsAllCachedPreviewsWhenAccessStops(statusCode: Int) async throws {
        let state = BrowserInspectorState()
        let status = ["tabs": tabs("docs", "mail")]
        state.applyStatus(status)
        state.recordPreview(NSImage(size: NSSize(width: 16, height: 10)), for: "mail")
        state.recordPreview(nil, for: "docs")
        state.visibleTabIDs = ["docs", "mail"]
        let statusData = try JSONSerialization.data(withJSONObject: status)
        let connection = BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            let url = try #require(request.url)
            if url.path == "/browser/view" {
                return (statusData, Self.response(url, code: 200))
            }
            #expect(url.path == "/browser/view/docs/image")
            return (Data(), Self.response(url, code: statusCode))
        }

        await state.refresh(using: connection, id: state.refreshID)

        #expect(state.privateInput == (statusCode == 423))
        #expect(state.groups.isEmpty)
        #expect(state.previews.isEmpty)
        #expect(state.previewIssues.isEmpty)
    }

    @Test @MainActor
    func freshnessMeasuresTimeSinceCapture() {
        let capturedAt = Date(timeIntervalSince1970: 100)
        let preview = BrowserPreview(image: NSImage(size: NSSize(width: 16, height: 10)), capturedAt: capturedAt)

        #expect(preview.freshness(at: capturedAt) == "Just now")
        #expect(preview.freshness(at: capturedAt + 4) == "Updated 4s ago")
        #expect(preview.freshness(at: capturedAt + 70) == "Updated 1m ago")
    }

    @Test(arguments: [200, 502, nil] as [Int?]) @MainActor
    func failedCaptureKeepsLastImageAndTimestamp(statusCode: Int?) async throws {
        let state = BrowserInspectorState()
        let image = NSImage(size: NSSize(width: 16, height: 10))
        let capturedAt = Date(timeIntervalSince1970: 100)
        state.recordPreview(image, for: "docs", at: capturedAt)
        let status = try JSONSerialization.data(withJSONObject: [
            "enabled": true, "connected": true, "tabs": tabs("docs"),
        ])
        let connection = BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            let url = try #require(request.url)
            if url.path == "/browser/view" {
                return (status, Self.response(url, code: 200))
            }
            #expect(url.path == "/browser/view/docs/image")
            guard let statusCode else { throw URLError(.timedOut) }
            return (Data(), Self.response(url, code: statusCode))
        }

        await state.refresh(using: connection, id: state.refreshID)

        #expect(state.previews["docs"]?.image === image)
        #expect(state.previews["docs"]?.capturedAt == capturedAt)
        #expect(state.previewIssues["docs"] == "Refresh failed.")

        let replacement = NSImage(size: NSSize(width: 16, height: 10))
        state.recordPreview(replacement, for: "docs", at: capturedAt + 4)
        #expect(state.previews["docs"]?.image === replacement)
        #expect(state.previews["docs"]?.capturedAt == capturedAt + 4)
        #expect(state.previewIssues["docs"] == nil)
    }

    @Test(arguments: [true, false]) @MainActor
    func rechecksAccessAfterAmbiguousCaptureFailure(extensionOffline: Bool) async throws {
        actor BrowserLink {
            var connected = true
            func disconnect() { connected = false }
        }
        let link = BrowserLink()
        let state = BrowserInspectorState()
        state.recordPreview(NSImage(size: NSSize(width: 16, height: 10)), for: "docs")
        let connected = try JSONSerialization.data(withJSONObject: [
            "enabled": true, "connected": true, "tabs": tabs("docs"),
        ])
        let disconnected = Data(#"{"enabled":true,"connected":false,"tabs":[]}"#.utf8)
        let connection = BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            let url = try #require(request.url)
            if url.path == "/browser/view" {
                if await link.connected { return (connected, Self.response(url, code: 200)) }
                if extensionOffline { return (disconnected, Self.response(url, code: 200)) }
                throw URLError(.notConnectedToInternet)
            }
            #expect(url.path == "/browser/view/docs/image")
            await link.disconnect()
            if extensionOffline { return (Data(), Self.response(url, code: 502)) }
            throw URLError(.notConnectedToInternet)
        }

        await state.refresh(using: connection, id: state.refreshID)

        #expect(state.emptyState == (extensionOffline ? .disconnected : nil))
        #expect(state.previews.isEmpty)
        #expect(state.previewIssues.isEmpty)
    }

    @Test @MainActor
    func emptyStateFollowsConnectionAccessAndTabCount() {
        let state = BrowserInspectorState()
        #expect(state.emptyState == nil)
        state.applyStatus(["enabled": true, "connected": false])
        #expect(state.emptyState == .disconnected)
        let refreshID = state.refreshID
        state.requestRefresh()
        #expect(state.refreshID == refreshID + 1)
        state.applyStatus(["enabled": false])
        #expect(state.emptyState == .disabled)
        state.applyStatus(["enabled": true, "connected": true])
        #expect(state.emptyState == .noTabs)
        state.applyStatus(["enabled": true, "connected": true, "tabs": tabs("docs")])
        #expect(state.emptyState == nil)

        state.recordPreview(NSImage(size: NSSize(width: 16, height: 10)), for: "docs")
        state.applyStatus(["enabled": false, "tabs": tabs("docs")])
        #expect(state.emptyState == .disabled)
        #expect(state.groups.isEmpty)
        #expect(state.previews.isEmpty)
    }

    private static func response(_ url: URL, code: Int) -> HTTPURLResponse {
        HTTPURLResponse(url: url, statusCode: code, httpVersion: nil, headerFields: nil)!
    }

    private func tabs(_ ids: String...) -> [[String: String]] {
        ids.map { ["id": $0, "title": $0] }
    }
}
