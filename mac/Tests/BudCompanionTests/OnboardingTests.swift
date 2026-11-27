import Foundation
import Testing
@testable import BudCompanion

@Suite struct OnboardingTests {
    @Test func parsesOnboardingStatusAndRejectsUnsafeAuthURL() throws {
        let raw: [String: Any] = [
            "has_model": false,
            "needs_onboarding": true,
            "active_model": NSNull(),
            "config_path": "/Users/example/.bud/config.env",
            "chatgpt": [
                "connected": false,
                "status": "waiting",
                "url": "http://insecure.example/auth",
                "models": ["gpt-5"],
            ],
            "keys": [
                "COMPOSIO_API_KEY": [
                    "configured": true,
                    "group": "integration",
                    "label": "Composio",
                    "description": "Gmail and Calendar",
                ],
                "OPENAI_API_KEY": [
                    "configured": false,
                    "group": "model",
                    "label": "OpenAI",
                    "description": "GPT models",
                ],
            ],
        ]

        let status = OnboardingStatus.parse(raw)
        #expect(status.hasModel == false)
        #expect(status.needsOnboarding == true)
        #expect(status.activeModel == nil)
        #expect(status.configPath == "/Users/example/.bud/config.env")
        #expect(status.chatgpt.status == "waiting")
        #expect(status.chatgpt.url == nil)
        #expect(status.modelKeys.map(\.envName) == ["OPENAI_API_KEY"])
        #expect(status.integrationKeys.map(\.envName) == ["COMPOSIO_API_KEY"])
        #expect(status.integrationKeys.first?.configured == true)

        let httpsURL = ChatGPTAuthStatus.safeHTTPSURL("https://auth.openai.com/oauth")
        #expect(httpsURL?.absoluteString == "https://auth.openai.com/oauth")
    }

    @Test @MainActor func saveKeyAndRemoveKeySendExpectedPayloadAndClearDrafts() async {
        var calls: [(String, String, [String: Any]?)] = []
        var syncedOnboarding: [Bool] = []

        let store = OnboardingStore(
            sendRequest: { method, path, body in
                calls.append((method, path, body))
                let isRemoval = (body?["keys"] as? [String: Any])?["OPENAI_API_KEY"] is NSNull
                return [
                    "has_model": !isRemoval,
                    "needs_onboarding": isRemoval,
                    "active_model": isRemoval ? NSNull() : "openai:gpt-5",
                    "config_path": "/tmp/config.env",
                    "chatgpt": ["connected": false, "status": "idle", "models": []],
                    "keys": [
                        "OPENAI_API_KEY": [
                            "configured": !isRemoval,
                            "group": "model",
                            "label": "OpenAI",
                            "description": "GPT models",
                        ]
                    ],
                ]
            },
            openURL: { _ in },
            syncNeedsOnboarding: { syncedOnboarding.append($0) }
        )

        store.beginKeyEdit("OPENAI_API_KEY")
        store.keyDrafts["OPENAI_API_KEY"] = "  sk-test-123  "
        await store.saveKey("OPENAI_API_KEY")

        #expect(calls.count == 1)
        #expect(calls[0].0 == "POST")
        #expect(calls[0].1 == "onboarding/keys")
        let sentKeys = calls[0].2?["keys"] as? [String: Any]
        #expect(sentKeys?["OPENAI_API_KEY"] as? String == "sk-test-123")
        #expect(store.keyDrafts["OPENAI_API_KEY"] == "")
        #expect(store.replacingKey == nil)
        #expect(store.status.hasModel == true)
        #expect(syncedOnboarding == [false])

        await store.removeKey("OPENAI_API_KEY")
        #expect(calls.count == 2)
        let removedKeys = calls[1].2?["keys"] as? [String: Any]
        #expect(removedKeys?["OPENAI_API_KEY"] is NSNull)
        #expect(store.status.hasModel == false)
        #expect(syncedOnboarding == [false, true])
    }

    @Test @MainActor func failedKeySaveKeepsDraftAndOnlyAnEditClearsItsError() async {
        let key = "OPENAI_API_KEY"
        let other = "DEEPSEEK_API_KEY"
        let store = OnboardingStore(
            sendRequest: { _, _, _ in throw ConnectionError.unreachable },
            openURL: { _ in },
            syncNeedsOnboarding: { _ in }
        )

        store.updateKeyDraft("test-draft", for: other)
        await store.saveKey(other)
        store.beginKeyEdit(key)
        store.updateKeyDraft("sk-test-123", for: key)
        await store.saveKey(key)

        #expect(store.replacingKey == key)
        #expect(store.keyDrafts[key] == "sk-test-123")
        #expect(store.error == nil)
        #expect(store.keyErrors[key] == "Couldn’t reach Bud. Try again.")
        store.updateKeyDraft("sk-test-123", for: key)
        #expect(store.keyErrors[key] != nil)
        store.updateKeyDraft("sk-test-edited", for: key)
        #expect(store.keyErrors[key] == nil)
        #expect(store.keyErrors[other] != nil)

        store.cancelKeyEdit()

        #expect(store.replacingKey == nil)
        #expect(store.keyDrafts[key] == "")
    }

    @Test(arguments: [true, false])
    @MainActor func rejectedModelSelectionRefreshesAndShowsSafeError(refreshSucceeds: Bool) async {
        let staleModel = "openai:gpt-5"
        let availableModel = "deepseek:deepseek-chat"
        let rejectionMessage = "That model is unavailable. Choose another model, sign in to ChatGPT, "
            + "or add its API key."
        var calls: [String] = []
        weak var observedStore: OnboardingStore?
        let store = OnboardingStore(
            sendRequest: { method, path, body in
                calls.append("\(method) \(path)")
                if method == "PUT" {
                    #expect(body?["model"] as? String == staleModel)
                    throw ConnectionError.http(422, "private provider detail")
                }
                if calls.count > 1 {
                    #expect(observedStore?.status.models.isEmpty == true)
                    #expect(observedStore?.error == rejectionMessage)
                    if !refreshSucceeds { throw ConnectionError.unreachable }
                }
                return ["models": calls.count == 1 ? [staleModel] : [availableModel]]
            },
            openURL: { _ in },
            syncNeedsOnboarding: { _ in }
        )
        observedStore = store

        await store.refresh()
        #expect(store.status.models == [staleModel])
        await store.chooseModel(staleModel)

        #expect(calls == ["GET onboarding", "PUT onboarding/model", "GET onboarding"])
        #expect(store.status.models == (refreshSucceeds ? [availableModel] : []))
        #expect(store.error == rejectionMessage)
        #expect(!store.changingModel)
    }

    @Test @MainActor func chatGPTLoginOpensURLAndLogoutResetsStatus() async {
        var opened: [URL] = []
        let store = OnboardingStore(
            sendRequest: { method, path, _ in
                if method == "POST" && path == "onboarding/chatgpt/start" {
                    return [
                        "has_model": false,
                        "needs_onboarding": true,
                        "config_path": "/tmp/config.env",
                        "chatgpt": [
                            "connected": false,
                            "status": "waiting",
                            "url": "https://auth.openai.com/authorize?test=1",
                            "models": [],
                        ],
                        "keys": [:],
                    ]
                }
                if method == "POST" && path == "onboarding/chatgpt/logout" {
                    return [
                        "has_model": false,
                        "needs_onboarding": true,
                        "config_path": "/tmp/config.env",
                        "chatgpt": ["connected": false, "status": "idle", "models": []],
                        "keys": [:],
                    ]
                }
                return [
                    "has_model": false,
                    "needs_onboarding": true,
                    "config_path": "/tmp/config.env",
                    "chatgpt": ["connected": false, "status": "idle", "models": []],
                    "keys": [:],
                ]
            },
            openURL: { opened.append($0) },
            syncNeedsOnboarding: { _ in }
        )

        await store.startChatGPTLogin()
        store.stopPolling()
        #expect(opened.map(\.absoluteString) == ["https://auth.openai.com/authorize?test=1"])
        #expect(store.status.chatgpt.status == "waiting")

        await store.logoutChatGPT()
        #expect(store.status.chatgpt.status == "idle")
        #expect(store.status.chatgpt.connected == false)
    }

    @Test @MainActor func refreshResumesPollingForWaitingChatGPTLogin() async throws {
        var requests = 0
        let store = OnboardingStore(
            sendRequest: { method, path, _ in
                #expect(method == "GET")
                #expect(path == "onboarding")
                requests += 1
                let connected = requests > 1
                return ["chatgpt": [
                    "connected": connected,
                    "status": connected ? "connected" : "waiting",
                ]]
            },
            openURL: { _ in },
            syncNeedsOnboarding: { _ in }
        )
        defer { store.stopPolling() }

        await store.refresh()
        #expect(store.status.chatgpt.status == "waiting")

        let clock = ContinuousClock()
        let deadline = clock.now + .seconds(5)
        while !store.status.chatgpt.connected, clock.now < deadline {
            try await clock.sleep(for: .milliseconds(25))
        }

        #expect(requests == 2)
        #expect(store.status.chatgpt.status == "connected")
        #expect(store.feedback == "Connected to ChatGPT.")
    }

    @Test(arguments: [true, false])
    @MainActor func modelSelectionConfirmsUpdate(running: Bool) async {
        let model = "openai:gpt-5"
        let store = OnboardingStore(
            sendRequest: { method, path, body in
                #expect(method == "PUT")
                #expect(path == "onboarding/model")
                #expect(body?["model"] as? String == model)
                return ["default_model": model, "models": [model]]
            },
            openURL: { _ in },
            syncNeedsOnboarding: { _ in },
            isRunning: { running }
        )

        await store.chooseModel(model)

        #expect(store.status.defaultModel == model)
        #expect(store.feedback == (running ? "Default updated for new runs." : "Default model updated."))
    }
}
