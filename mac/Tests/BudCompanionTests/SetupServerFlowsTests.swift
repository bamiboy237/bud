import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct SetupServerFlowsTests {
    @Test
    func browserStatusDistinguishesLoadingEmptyAndUnavailable() {
        let settings = ComputerSettingsState()
        #expect(settings.browserStatus == "Checking browsers…")
        settings.updateBrowsers(["browsers": [[String: String]]()])
        #expect(settings.browserStatus == "No connected browsers")
        settings.updateBrowsers([
            "browsers": [["id": "mac", "name": "Chrome"]],
            "available": true,
        ])
        #expect(settings.browserStatus == nil)

        settings.updateBrowsers([:])
        #expect(settings.browserStatus == "Couldn't load browsers")
        #expect(settings.computers.map(\.id) == ["mac"])
        #expect(!settings.canPair)

        settings.updateBrowsers(["browsers": [[String: String]]()])
        #expect(settings.browserStatus == "No connected browsers")
    }

    @Test
    func pairingWaitsForConfirmationOfItsCode() {
        let settings = ComputerSettingsState()
        settings.pairing = .init(destination: .daytona, code: "DAYTONA-CODE", address: "100.64.1.2:19989")
        #expect(settings.pairing?.title == "Pair Daytona")

        let browsers = [["id": "other", "name": "Chrome"], ["id": "vm", "name": "Daytona"]]
        settings.updateBrowsers(["browsers": browsers])
        #expect(settings.pairing?.connectedBrowser == nil)
        settings.updateBrowsers([
            "browsers": browsers,
            "pairing": ["code": "ANOTHER-CODE", "id": "vm", "name": "Daytona"],
        ])
        #expect(settings.pairing?.connectedBrowser == nil)
        settings.updateBrowsers([
            "browsers": browsers,
            "pairing": ["code": "DAYTONA-CODE", "name": "Daytona"],
        ])
        #expect(settings.pairing?.connectedBrowser == nil)

        settings.updateBrowsers([
            "browsers": browsers,
            "pairing": ["code": "DAYTONA-CODE", "id": "vm", "name": "Work browser"],
        ])
        #expect(settings.pairing?.connectedBrowser?.id == "vm")
        #expect(settings.pairing?.title == "Connected to Work browser")
        settings.updateBrowsers(["browsers": browsers])
        #expect(settings.pairing?.title == "Connected to Work browser")

        settings.pairing = .init(destination: .chrome, code: "CHROME-CODE", address: "127.0.0.1:19989")
        #expect(settings.pairing?.title == "Pair Chrome")
    }

    @Test
    func connectionValidationRequestsFocusAndClearsErrorsOnEdit() {
        let draft = ServerSettingsDraft()
        draft.address = "http://bud.example.test"
        draft.token = "invalid"
        #expect(draft.connect {} == nil)
        #expect(draft.addressError != nil)
        #expect(draft.fieldFocus?.field == .address)
        #expect(draft.tokenError == nil)
        let firstFocus = draft.fieldFocus
        #expect(draft.connect {} == nil)
        #expect(draft.fieldFocus != firstFocus)

        draft.address = "https://bud.example.test"
        #expect(draft.addressError == nil)
        #expect(draft.connect {} == nil)
        #expect(draft.tokenError != nil)
        #expect(draft.fieldFocus?.field == .token)
        #expect(draft.generalError == nil)

        draft.token = String(repeating: "A", count: 43)
        #expect(draft.tokenError == nil)
    }

    @Test(arguments: [401, 403, 409])
    func serverRejectionAppearsBesideTheRelevantControl(status: Int) async throws {
        let draft = ServerSettingsDraft(
            verify: { _ in throw ConnectionError.http(status, "Server response") },
            save: { _ in Issue.record("Verification failed before save") }
        )
        draft.address = "https://bud.example.test"
        draft.token = String(repeating: "A", count: 43)
        let check = try #require(draft.connect { Issue.record("Verification failed before connect") })
        await check.value

        #expect(draft.addressError == nil)
        if status == 409 {
            #expect(draft.generalError == "Server response")
            #expect(draft.tokenError == nil)
            #expect(draft.fieldFocus == nil)
        } else {
            #expect(draft.generalError == nil)
            #expect(draft.tokenError != nil)
            #expect(draft.fieldFocus?.field == .token)
            draft.token = String(repeating: "B", count: 43)
            #expect(draft.tokenError == nil)
        }
    }

    @Test
    func moveStagesArriveBeforeTheCLIExits() async throws {
        let folder = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: folder) }
        let script = #"""
            read -r token || exit 1
            test "$BUD_MOVE_PROGRESS" = 1 || exit 1
            for stage in packing uploading starting; do
                printf '{"stage":"%s"}\n' "$stage" >&2
                attempts=0
                until [ -f "$1/$stage" ]; do
                    if [ "$attempts" -eq 200 ]; then
                        printf 'No live update for %s\n' "$stage" >&2
                        exit 1
                    fi
                    attempts=$((attempts + 1))
                    sleep 0.01
                done
            done
            printf '{"state":"complete"}\n'
            """#
        var labels: [String] = []
        let reply = try await MoveBudDraft.runCLI(
            URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", script, "test-cli", folder.path()],
            token: "test-token"
        ) { stage in
            labels.append(stage.label(server: "bud.example.test"))
            _ = FileManager.default.createFile(atPath: folder.appending(path: stage.rawValue).path(),
                                               contents: nil)
        }
        #expect(labels == [
            "Packing your files", "Uploading to bud.example.test", "Starting Bud on bud.example.test",
        ])
        #expect(reply.state == "complete")
    }

    @Test(arguments: [false, true])
    func cancelledVerificationDiscardsLateReplies(returnsError: Bool) async throws {
        let (requests, started) = AsyncStream<URL>.makeStream()
        var pending: [URL: CheckedContinuation<Void, Error>] = [:]
        var saved: [URL] = []
        var connections = 0
        let draft = ServerSettingsDraft(
            verify: { connection in
                try await withCheckedThrowingContinuation { continuation in
                    pending[connection.base] = continuation
                    started.yield(connection.base)
                }
            },
            save: { saved.append($0.base) }
        )
        draft.address = "https://first.example.test"
        draft.token = String(repeating: "A", count: 43)
        var iterator = requests.makeAsyncIterator()
        let first = try #require(draft.connect { connections += 1 })
        let firstRequest = await iterator.next()
        let firstURL = try #require(firstRequest)
        let oldRequest = try #require(pending[firstURL])
        #expect(draft.checking)
        draft.cancelCheck()
        #expect(first.isCancelled)
        #expect(!draft.checking)

        draft.address = "https://second.example.test"
        let second = try #require(draft.connect { connections += 1 })
        let secondRequest = await iterator.next()
        let secondURL = try #require(secondRequest)
        let newRequest = try #require(pending[secondURL])
        if returnsError {
            oldRequest.resume(throwing: ConnectionError.unreachable)
        } else {
            oldRequest.resume()
        }
        await first.value
        #expect(draft.checking)
        #expect(draft.generalError == nil)
        #expect(saved.isEmpty)
        #expect(connections == 0)

        newRequest.resume()
        await second.value
        #expect(saved == [secondURL])
        #expect(connections == 1)
        #expect(!draft.checking)
    }
}
