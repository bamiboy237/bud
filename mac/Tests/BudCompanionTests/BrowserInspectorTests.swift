import AppKit
import Testing
@testable import BudCompanion

struct BrowserInspectorTests {
    @Test
    func chromeHandoffAndManualReturnUseSeparateActions() {
        #expect(ChromePause(["private": true, "handoff_open": true]) == .handoff)
        #expect(ChromePause(["private": true, "handoff_open": false]) == .manual)
        #expect(ChromePause(["private": true, "interactive": true]) == nil)
        #expect(ChromePause(["private": false]) == nil)
    }

    @Test(arguments: ["about:blank", "ABOUT:blank", "", " "])
    func blankTitlesUseTheTabHost(title: String) throws {
        let tab = try #require(BrowserTab(["id": "docs", "title": title, "host": "docs.example"]))
        #expect(tab.displayTitle == "docs.example")
    }

    @Test
    func normalizesClicksAcrossThePreviewImage() {
        let bounds = CGRect(x: 20, y: 10, width: 320, height: 160)
        #expect(normalizedBrowserPoint(CGPoint(x: 20, y: 10), in: bounds) == .zero)
        #expect(normalizedBrowserPoint(CGPoint(x: 100, y: 130), in: bounds) == CGPoint(x: 0.25, y: 0.75))
        #expect(normalizedBrowserPoint(CGPoint(x: 370, y: 190), in: bounds) == CGPoint(x: 1, y: 1))
        #expect(normalizedBrowserPoint(.zero, in: .zero) == nil)
    }

    @Test @MainActor
    func interactiveTakeoverKeepsTheSelectedPreviewAndRefreshesOnlyThatTab() async throws {
        let state = BrowserInspectorState()
        state.applyStatus(["tabs": tabs("docs", "mail"), "active_tab": "mail"])
        state.select("docs")
        state.visibleTabIDs = ["docs", "mail"]
        let image = NSImage(size: NSSize(width: 16, height: 10), flipped: false) { bounds in
            NSColor.white.setFill()
            NSBezierPath(rect: bounds).fill()
            return true
        }
        state.recordPreview(image, for: "docs")
        state.recordPreview(image, for: "mail")
        let imageData = try #require(image.tiffRepresentation)
        let docTab = try #require(state.groups.first?.tabs.first)
        let connection = BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            let url = try #require(request.url)
            let response: Data
            switch (request.httpMethod, url.path) {
            case ("POST", "/browser/view/takeover"):
                let body = try #require(request.httpBody)
                let json = try JSONSerialization.jsonObject(with: body) as? [String: String]
                #expect(json == ["tab": "docs"])
                response = Data(#"{"private":true,"interactive":true,"tab":"docs","handoff_open":false}"#.utf8)
            case ("POST", "/browser/view/control/click"):
                let body = try #require(request.httpBody)
                let json = try JSONSerialization.jsonObject(with: body) as? [String: Any]
                #expect(json?["tab"] as? String == "docs")
                #expect(json?["nx"] as? Double == 0.25)
                #expect(json?["ny"] as? Double == 0.75)
                response = Data("{}".utf8)
            case ("GET", "/browser/view"):
                response = Data(#"{"private":true,"interactive":true,"takeover_tab":"docs","tabs":[]}"#.utf8)
            case ("GET", "/browser/view/docs/image"):
                response = imageData
            default:
                Issue.record("Unexpected browser request \(request)")
                return (Data(), Self.response(url, code: 404))
            }
            return (response, Self.response(url, code: 200))
        }

        await state.takeOver(docTab, using: connection)

        #expect(state.control == .interactive(tabID: "docs", title: "docs"))
        #expect(state.previews["docs"]?.image === image)
        #expect(state.previews["mail"] == nil)
        let refreshID = state.refreshID
        let sent = await state.sendControl("click", tabID: "docs",
                                           body: ["nx": 0.25, "ny": 0.75], using: connection)
        #expect(sent)
        #expect(state.refreshID == refreshID + 1)
        await state.refresh(using: connection, id: state.refreshID)
        #expect(state.previews["docs"]?.image !== image)
        #expect(state.previews["docs"] != nil)
    }

    @Test(arguments: [true, false]) @MainActor
    func giveBackControlKeepsControlOnFailureAndReturnsToTheDeckOnSuccess(interactive: Bool) async throws {
        let state = BrowserInspectorState()
        if interactive {
            state.applyStatus(["private": true, "interactive": true, "takeover_tab": "docs"])
            #expect(state.interactiveTabID == "docs")
        } else {
            state.applyStatus(["private": true, "handoff_open": true])
        }
        let control = try #require(state.control)
        let base = URL(string: "https://bud.example")!
        let rejected = BudConnection(base: base, token: "") { request in
            let url = try #require(request.url)
            #expect(request.httpMethod == "POST")
            #expect(url.path == "/browser/view/resume")
            return (Data(#"{"detail":"Try again."}"#.utf8), Self.response(url, code: 409))
        }
        let status = try JSONSerialization.data(withJSONObject: [
            "enabled": true, "connected": true, "tabs": tabs("docs", "mail"),
        ])
        let accepted = BudConnection(base: base, token: "") { request in
            let url = try #require(request.url)
            switch (request.httpMethod, url.path) {
            case ("POST", "/browser/view/resume"):
                return (Data(#"{"private":false}"#.utf8), Self.response(url, code: 200))
            case ("GET", "/browser/view"):
                return (status, Self.response(url, code: 200))
            default:
                #expect(url.path == "/browser/view/docs/image")
                return (Data(), Self.response(url, code: 200))
            }
        }

        await state.resume(using: rejected)
        #expect(state.control == control)
        #expect(state.problem == "Try again.")
        #expect(!state.sending)

        let refreshID = state.refreshID
        await state.resume(using: accepted)
        #expect(state.control == nil)
        #expect(state.refreshID == refreshID + 1)
        await state.refresh(using: accepted, id: state.refreshID)
        #expect(state.groups.flatMap(\.tabs).map(\.id) == ["docs", "mail"])
    }

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
