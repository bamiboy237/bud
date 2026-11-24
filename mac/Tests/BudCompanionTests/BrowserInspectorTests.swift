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
}
