import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct ConnectedAppsSettingsTests {
    @Test
    func showsLiveStatusAndSearchesByAppOrAccountName() async {
        let state = ConnectedAppsSettingsState(sendRequest: { _, _, _ in
            ["accounts": [
                ["toolkit": "googlecalendar", "display_name": "Work calendar", "status": "expired"],
                ["toolkit": "gmail", "display_name": "Gmail", "status": "connected"],
                ["toolkit": "github", "display_name": "GitHub", "status": "revoked"],
                ["toolkit": "slack", "display_name": "Slack", "status": "initiated"],
            ]]
        })
        await state.refresh()
        #expect(state.filteredAccounts.map(\.status.rawValue) == [
            "Revoked", "Connected", "Needs sign-in", "Expired",
        ])

        state.search = " google calendar "
        #expect(state.filteredAccounts.map(\.name) == ["Work calendar"])
        state.search = "work CALENDAR"
        #expect(state.filteredAccounts.map(\.name) == ["Work calendar"])
        state.search = "missing"
        #expect(state.filteredAccounts.isEmpty)
    }

    @Test
    func reconnectsInBrowserAndReloadsAfterEachChange() async throws {
        var requests: [String] = []
        var opened: [URL] = []
        var accountStatus: String? = "connected"
        let state = ConnectedAppsSettingsState(sendRequest: { method, path, _ in
            requests.append("\(method) /\(path)")
            switch path {
            case "connections/gmail/connect":
                accountStatus = "initiated"
                return ["url": "https://example.test/sign-in"]
            case "connections/gmail/disconnect":
                accountStatus = nil
                return ["disconnected": true]
            default:
                let accounts = accountStatus.map { [["toolkit": "gmail", "status": $0]] } ?? []
                return ["accounts": accounts]
            }
        }, openBrowser: { url in
            opened.append(url)
            return true
        })

        await state.refresh()
        let linked = try #require(state.filteredAccounts.first)
        await state.change(.reconnect, account: linked)
        #expect(opened.map(\.absoluteString) == ["https://example.test/sign-in"])
        let signingIn = try #require(state.filteredAccounts.first)
        #expect(signingIn.status == .needsSignIn)

        await state.change(.disconnect, account: signingIn)
        #expect(state.accounts?.isEmpty == true)
        #expect(requests == [
            "GET /connections", "POST /connections/gmail/connect", "GET /connections",
            "POST /connections/gmail/disconnect", "GET /connections",
        ])
    }

    @Test
    func readsOlderServerStatusesWithoutAddingUnlinkedApps() throws {
        let accounts = try ConnectedAppAccount.decode(["statuses": ["gmail": "NOT_CONNECTED", "calendar": "ACTIVE"]])
        #expect(accounts.map(\.name) == ["Google Calendar"])
        #expect(accounts.map(\.status) == [.connected])
    }
}
