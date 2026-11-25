import Foundation
import Testing
@testable import BudCompanion

@Suite struct UpdaterTests {
    @Test func semanticVersionOrdersReleasesAndPrereleasesCleanly() throws {
        let v010 = try #require(SemanticVersion("0.1.0"))
        let v020rc1 = try #require(SemanticVersion("v0.2.0-rc1"))
        let v020 = try #require(SemanticVersion("v0.2.0"))
        let v021 = try #require(SemanticVersion("0.2.1"))

        #expect(v010 < v020rc1)
        #expect(v020rc1 < v020)
        #expect(v020 < v021)
        #expect(SemanticVersion("v0.1.0") == v010)
        #expect(SemanticVersion("not-a-version") == nil)
    }

    @Test func parsesGitHubReleaseMetadataJSON() throws {
        let raw = """
        {
          "tag_name": "v0.3.2",
          "html_url": "https://github.com/bamiboy237/bud/releases/tag/v0.3.2"
        }
        """.data(using: .utf8)!
        let parsed = try #require(AppUpdater.parseLatestReleaseJSON(raw))
        #expect(parsed.version == "0.3.2")
        #expect(parsed.url?.absoluteString == "https://github.com/bamiboy237/bud/releases/tag/v0.3.2")
    }

    @Test @MainActor func updaterCheckTransitionsToAvailableAndUpToDate() async throws {
        let newerPayload = """
        {"tag_name": "v9.9.9", "html_url": "https://github.com/bamiboy237/bud/releases/tag/v9.9.9"}
        """.data(using: .utf8)!
        let url = AppUpdater.latestReleaseURL
        let response = HTTPURLResponse(
            url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!

        let updater = AppUpdater { _ in (newerPayload, response) }
        await updater.checkForUpdates()
        #expect(updater.state == .available(
            version: "9.9.9",
            releaseURL: URL(string: "https://github.com/bamiboy237/bud/releases/tag/v9.9.9")!
        ))

        let olderPayload = """
        {"tag_name": "v0.0.1", "html_url": "https://github.com/bamiboy237/bud/releases/tag/v0.0.1"}
        """.data(using: .utf8)!
        let upToDateUpdater = AppUpdater { _ in (olderPayload, response) }
        await upToDateUpdater.checkForUpdates()
        #expect(upToDateUpdater.state == .upToDate(current: AppUpdater.currentAppVersion()))
    }

    @Test func ownerTokenFallsBackToInstalledDotBudHomeAndConfigEnv() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("bud-conn-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }

        let fakeUserHome = root.appendingPathComponent("user")
        let dotBud = fakeUserHome.appendingPathComponent(".bud")
        let emptyCwd = root.appendingPathComponent("empty-cwd")
        let repoCwd = root.appendingPathComponent("repo")

        try FileManager.default.createDirectory(
            at: dotBud.appendingPathComponent("custom-data/auth"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: emptyCwd, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: repoCwd.appendingPathComponent("data/auth"), withIntermediateDirectories: true)

        let homeToken = String(repeating: "h", count: 43)
        let repoToken = String(repeating: "r", count: 43)
        try "DATA_DIR=custom-data\n".write(
            to: dotBud.appendingPathComponent("config.env"), atomically: true, encoding: .utf8)
        try homeToken.write(
            to: dotBud.appendingPathComponent("custom-data/auth/owner.token"),
            atomically: true,
            encoding: .utf8)
        try repoToken.write(
            to: repoCwd.appendingPathComponent("data/auth/owner.token"),
            atomically: true,
            encoding: .utf8)

        let fromInstalled = try BudConnection.ownerToken(
            home: "",
            currentDirectory: emptyCwd,
            userHome: fakeUserHome,
            environment: [:]
        )
        #expect(fromInstalled == homeToken)

        let fromCheckout = try BudConnection.ownerToken(
            home: "",
            currentDirectory: repoCwd,
            userHome: fakeUserHome,
            environment: [:]
        )
        #expect(fromCheckout == repoToken)
    }

    @Test func installedAppUsesItsServiceInsteadOfAnOldCheckoutFolder() throws {
        let serviceHome = URL(filePath: "/Users/example/.bud")
        let service = LocalBudService(home: serviceHome, port: 8420)
        let stale = BudSettings(url: BudSettings.defaultURL, home: "/Users/example/Desktop/bud")

        let selected = stale.selectingLocalService(service, isDevBuild: false, environment: [:])
        #expect(selected.home == serviceHome.path)
        #expect(selected.url == BudSettings.defaultURL)
        #expect(selected.localService == service)
    }

    @Test func installedServiceDoesNotReplaceExplicitDevOrRemoteConnection() throws {
        let service = LocalBudService(home: URL(filePath: "/Users/example/.bud"), port: 8420)
        let local = BudSettings(url: BudSettings.defaultURL, home: "/repo")
        let remote = BudSettings(url: "https://bud.example.net", home: "/remote")

        let dev = local.selectingLocalService(service, isDevBuild: true, environment: [:])
        #expect(dev.home == "/repo")
        #expect(dev.localService == nil)
        let explicit = local.selectingLocalService(
            service, isDevBuild: false, environment: ["BUD_HOME": "/repo"])
        #expect(explicit.home == "/repo")
        #expect(explicit.localService == nil)
        let selectedRemote = remote.selectingLocalService(service, isDevBuild: false, environment: [:])
        #expect(selectedRemote.url == remote.url)
        #expect(selectedRemote.home == remote.home)
        #expect(selectedRemote.localService == nil)
    }

    @Test func localServiceStartsWithoutRestartingAnAlreadyLoadedAgent() async throws {
        let userHome = URL(filePath: "/Users/example")
        var commands: [[String]] = []
        try await LocalBudService.start(userHome: userHome, uid: 501) { command in
            commands.append(command)
            return command.first == "kickstart" ? 1 : 0
        }
        #expect(commands == [
            ["kickstart", "gui/501/app.bud.server"],
            ["bootstrap", "gui/501", "/Users/example/Library/LaunchAgents/app.bud.server.plist"],
        ])
        commands.removeAll()
        try await LocalBudService.start(userHome: userHome, uid: 501) { command in
            commands.append(command)
            return 0
        }
        #expect(commands == [["kickstart", "gui/501/app.bud.server"]])
    }

    @Test func remoteServerRequiresAnHTTPSOrigin() {
        #expect(RemoteBudServer.origin(" https://Bud.Example.com:443/ ")?.absoluteString
                == "https://bud.example.com")
        #expect(RemoteBudServer.origin("https://bud.example.com:8443")?.port == 8443)
        for address in [
            "http://localhost:8420", "https://bud.example.com/chat", "https://bud.example.com/#home",
            "https://user:password@bud.example.com", "https://bud.example.com?secret=1",
        ] {
            #expect(RemoteBudServer.origin(address) == nil)
        }
    }

    @Test func diagnosticsFormatterIncludesServerVersionWhenPresent() {
        let snapshot = DiagnosticsSnapshot(
            appVersion: "0.1.0",
            appBuild: "1",
            macOSVersion: "Version 15.2",
            connectionPhase: "connected",
            connectionAttempt: 1,
            backoffAttempt: 0,
            sessionID: "s1",
            serverURL: "http://127.0.0.1:8420",
            petStatus: "idle",
            running: false,
            pendingApprovalsCount: 0,
            pendingSetupsCount: 0,
            activeTasksCount: 0,
            stalledTasksCount: 0,
            decidingCount: 0,
            approvingBatch: false,
            voiceState: "off",
            voiceKey: "⌥ Space",
            needsAccessibility: false,
            recentNotices: [],
            recentErrors: [],
            serverVersion: "0.2.0"
        )
        let formatted = DiagnosticsFormatter.format(snapshot)
        #expect(formatted.contains("Server version: 0.2.0"))
    }
}
