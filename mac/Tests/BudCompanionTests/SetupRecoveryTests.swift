import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct SetupRecoveryTests {
    @Test(arguments: [false, true])
    func reopenUsesServerLinkAndAsksBeforeReplacingAnExpiredLink(expires: Bool) async throws {
        let base = try #require(URL(string: "https://bud.example.test"))
        let newLink = "https://accounts.example.test/fresh"
        let companion = Companion()
        companion.connectForTesting(BudConnection(base: base, token: "test", transport: { request in
            let url = try #require(request.url)
            #expect(url.path == "/connections/github/connect")
            #expect(request.httpMethod == "POST")
            let data = try #require(request.httpBody)
            let body = try #require(JSONSerialization.jsonObject(with: data) as? [String: Bool])
            #expect(body["resume"] == true)
            let refresh = try #require(body["refresh"] as Bool?)
            #expect(expires || !refresh)
            let reply: [String: Any] = expires && !refresh
                ? ["expired": true]
                : ["url": newLink, "expired": false]
            let response = try #require(HTTPURLResponse(
                url: url, statusCode: 200, httpVersion: nil, headerFields: nil))
            return (try JSONSerialization.data(withJSONObject: reply), response)
        }))
        companion.receiveSetup([
            "capability": "github", "setup_id": "setup-1", "kind": "url", "status": "waiting",
            "url": "https://accounts.example.test/cached",
        ], replacing: true)
        defer { companion.receiveSetup(["capability": "github", "status": "cancelled"]) }
        let setup = try #require(companion.setups.first)
        let recovery = SetupSignInRecovery()
        var opened: [String] = []
        let recordOpen: @MainActor (URL) -> Bool = { url in
            opened.append(url.absoluteString)
            return true
        }

        await recovery.openLink(for: setup, using: companion, openBrowser: recordOpen)
        #expect(recovery.needsNewLink(for: setup) == expires)
        if expires {
            #expect(opened.isEmpty)
            await recovery.openLink(for: setup, using: companion, openBrowser: recordOpen)
        }

        #expect(opened == [newLink])
        #expect(!recovery.needsNewLink(for: setup))
        #expect(companion.openedSetups == ["github"])
        #expect(companion.setups.first?.setupID == "setup-1")
    }

    @Test
    func checkSignInImmediatelyRequestsSetupStatus() async throws {
        let (requests, recorded) = AsyncStream<URLRequest>.makeStream()
        let companion = Companion()
        companion.receiveSetup(["capability": "github", "kind": "url", "status": "waiting"])
        defer { companion.receiveSetup(["capability": "github", "status": "cancelled"]) }
        let base = try #require(URL(string: "https://bud.example.test"))
        companion.connectForTesting(BudConnection(base: base, token: "test", transport: { request in
            recorded.yield(request)
            let url = try #require(request.url)
            let response = try #require(HTTPURLResponse(
                url: url, statusCode: 200, httpVersion: nil, headerFields: nil))
            return (Data(#"{"capability":"github","status":"connected"}"#.utf8), response)
        }))
        companion.link = .connected

        SetupSignInRecovery().check(using: companion)

        var next = requests.makeAsyncIterator()
        let request = await next.next()
        #expect(request?.httpMethod == "POST")
        #expect(request?.url?.path == "/sessions/s1/setup/github/check")
    }
}
