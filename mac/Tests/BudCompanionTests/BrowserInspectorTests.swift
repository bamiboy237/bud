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
        state.screenshots = ["docs": preview, "mail": preview]
        state.previewIssues = ["notes": "Preview unavailable.", "calendar": "Preview unavailable."]

        state.applyStatus(["tabs": tabs("docs", "notes")])

        #expect(Set(state.screenshots.keys) == ["docs"])
        #expect(state.screenshots["docs"] === preview)
        #expect(Set(state.previewIssues.keys) == ["notes"])
    }

    @Test @MainActor
    func clearsAllPreviewsOnPrivateStatus() {
        let state = BrowserInspectorState()
        state.applyStatus(["tabs": tabs("docs", "mail"), "active_tab": "docs"])
        state.screenshots["docs"] = NSImage(size: NSSize(width: 16, height: 10))
        state.previewIssues["mail"] = "Preview unavailable."

        state.applyStatus(["private": true, "handoff_open": true])

        #expect(state.privateInput)
        #expect(state.handoffOpen)
        #expect(state.groups.isEmpty)
        #expect(state.activeTabID == nil)
        #expect(state.screenshots.isEmpty)
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
        state.screenshots["mail"] = preview
        let refreshID = state.refreshID
        state.select("docs")
        #expect(state.focus.selected == "docs")
        #expect(state.refreshID != refreshID)
        #expect(state.screenshots["mail"] === preview)
        state.applyStatus(status)
        #expect(state.focus.selected == "docs")
        #expect(state.activeTabID == "mail")

        state.applyStatus(["tabs": tabs("docs", "mail")])
        #expect(state.activeTabID == nil)
    }

    @Test(arguments: [true, false]) @MainActor
    func dropsAllCachedPreviewsWhenCaptureStops(privateInput: Bool) async throws {
        let state = BrowserInspectorState()
        let status = ["tabs": tabs("docs", "mail")]
        state.applyStatus(status)
        state.screenshots["mail"] = NSImage(size: NSSize(width: 16, height: 10))
        state.previewIssues["docs"] = "Preview unavailable."
        state.visibleTabIDs = ["docs", "mail"]
        let statusData = try JSONSerialization.data(withJSONObject: status)
        let connection = BudConnection(base: URL(string: "https://bud.example")!, token: "") { request in
            let url = try #require(request.url)
            if url.path == "/browser/view" {
                return (statusData, HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            }
            #expect(url.path == "/browser/view/docs/image")
            if privateInput {
                return (Data(), HTTPURLResponse(url: url, statusCode: 423, httpVersion: nil, headerFields: nil)!)
            }
            throw URLError(.notConnectedToInternet)
        }

        await state.refresh(using: connection, id: state.refreshID)

        #expect(state.privateInput == privateInput)
        #expect(state.groups.isEmpty)
        #expect(state.screenshots.isEmpty)
        #expect(state.previewIssues.isEmpty)
    }

    private func tabs(_ ids: String...) -> [[String: String]] {
        ids.map { ["id": $0, "title": $0] }
    }
}
