import AppKit
import Foundation
import Observation
import SwiftUI

struct ManagedKeyStatus: Identifiable, Equatable, Sendable {
    let envName: String
    let label: String
    let description: String
    let group: String
    let configured: Bool

    var id: String { envName }
}

struct ChatGPTAuthStatus: Equatable, Sendable {
    let connected: Bool
    let status: String
    let url: URL?
    let error: String?
    let selectedModel: String?
    let models: [String]

    static let empty = ChatGPTAuthStatus(
        connected: false,
        status: "idle",
        url: nil,
        error: nil,
        selectedModel: nil,
        models: []
    )

    static func safeHTTPSURL(_ text: String?) -> URL? {
        guard let text, let url = URL(string: text),
              url.scheme == "https", url.host != nil, url.user == nil, url.password == nil
        else { return nil }
        return url
    }
}

struct OnboardingStatus: Equatable, Sendable {
    let hasModel: Bool
    let needsOnboarding: Bool
    let activeModel: String?
    let defaultModel: String?
    let models: [String]
    let modelLocked: Bool
    let configPath: String
    let chatgpt: ChatGPTAuthStatus
    let keys: [ManagedKeyStatus]

    static let empty = OnboardingStatus(
        hasModel: false,
        needsOnboarding: false,
        activeModel: nil,
        defaultModel: nil,
        models: [],
        modelLocked: false,
        configPath: "~/.bud/config.env",
        chatgpt: .empty,
        keys: []
    )

    private static let keyOrder = [
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "MUSE_API_KEY",
        "EXA_API_KEY",
        "COMPOSIO_API_KEY",
    ]

    var modelKeys: [ManagedKeyStatus] {
        keys.filter { $0.group == "model" }
    }

    var integrationKeys: [ManagedKeyStatus] {
        keys.filter { $0.group == "integration" }
    }

    static func parse(_ raw: [String: Any]) -> OnboardingStatus {
        let hasModel = raw["has_model"] as? Bool ?? false
        let needsOnboarding = raw["needs_onboarding"] as? Bool ?? !hasModel
        let activeRaw = raw["active_model"] as? String
        let activeModel = (activeRaw?.isEmpty == false) ? activeRaw : nil
        let configPath = (raw["config_path"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            ?? "~/.bud/config.env"

        let cg = raw["chatgpt"] as? [String: Any] ?? [:]
        let connected = cg["connected"] as? Bool ?? false
        let status = cg["status"] as? String ?? (connected ? "connected" : "idle")
        let selectedRaw = cg["selected_model"] as? String
        let selectedModel = (selectedRaw?.isEmpty == false) ? selectedRaw : nil
        let chatgpt = ChatGPTAuthStatus(
            connected: connected,
            status: status,
            url: ChatGPTAuthStatus.safeHTTPSURL(cg["url"] as? String),
            error: cg["error"] as? String,
            selectedModel: selectedModel,
            models: cg["models"] as? [String] ?? []
        )

        let rawKeys = raw["keys"] as? [String: Any] ?? [:]
        var parsedKeys: [ManagedKeyStatus] = []
        for (envName, entryAny) in rawKeys {
            guard let entry = entryAny as? [String: Any] else { continue }
            parsedKeys.append(
                ManagedKeyStatus(
                    envName: envName,
                    label: entry["label"] as? String ?? envName,
                    description: entry["description"] as? String ?? "",
                    group: entry["group"] as? String ?? "model",
                    configured: entry["configured"] as? Bool ?? false
                )
            )
        }
        parsedKeys.sort { lhs, rhs in
            let lIdx = keyOrder.firstIndex(of: lhs.envName) ?? Int.max
            let rIdx = keyOrder.firstIndex(of: rhs.envName) ?? Int.max
            if lIdx != rIdx { return lIdx < rIdx }
            return lhs.envName < rhs.envName
        }

        return OnboardingStatus(
            hasModel: hasModel,
            needsOnboarding: needsOnboarding,
            activeModel: activeModel,
            defaultModel: raw["default_model"] as? String,
            models: raw["models"] as? [String] ?? [],
            modelLocked: raw["model_locked"] as? Bool ?? false,
            configPath: configPath,
            chatgpt: chatgpt,
            keys: parsedKeys
        )
    }
}

@MainActor @Observable
final class OnboardingStore {
    typealias Transport = @MainActor (String, String, [String: Any]?) async throws -> [String: Any]
    typealias URLOpener = @MainActor (URL) -> Void
    typealias NeedsOnboardingSink = @MainActor (Bool) -> Void

    static let shared = OnboardingStore()

    var status = OnboardingStatus.empty
    var apiKeysExpanded = false
    var keyDrafts: [String: String] = [:]
    var loading = false
    var changingModel = false
    var savingKey: String? = nil
    var busyChatGPT = false
    var feedback: String? = nil
    var error: String? = nil

    @ObservationIgnored private let sendRequest: Transport
    @ObservationIgnored private let openURL: URLOpener
    @ObservationIgnored private let syncNeedsOnboarding: NeedsOnboardingSink
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var initializedExpansion = false
    @ObservationIgnored private var serverGeneration = 0

    init(
        sendRequest: @escaping Transport = { method, path, json in
            try await Companion.shared.sendOnboardingRequest(method, path, json: json)
        },
        openURL: @escaping URLOpener = { NSWorkspace.shared.open($0) },
        syncNeedsOnboarding: @escaping NeedsOnboardingSink = { Companion.shared.needsOnboarding = $0 }
    ) {
        self.sendRequest = sendRequest
        self.openURL = openURL
        self.syncNeedsOnboarding = syncNeedsOnboarding
    }

    func clearBanner() {
        error = nil
        feedback = nil
    }

    func reset() {
        serverGeneration += 1
        stopPolling()
        status = .empty
        keyDrafts = [:]
        apiKeysExpanded = false
        initializedExpansion = false
        clearBanner()
    }

    func refresh() async {
        loading = true
        error = nil
        defer { loading = false }
        let generation = serverGeneration
        do {
            let raw = try await sendRequest("GET", "onboarding", nil)
            guard generation == serverGeneration else { return }
            apply(raw)
        } catch {
            guard generation == serverGeneration else { return }
            self.error = "\(error)"
        }
    }

    func startChatGPTLogin() async {
        stopPolling()
        busyChatGPT = true
        error = nil
        feedback = nil
        defer { busyChatGPT = false }
        do {
            let raw = try await sendRequest("POST", "onboarding/chatgpt/start", nil)
            apply(raw)
            if let url = status.chatgpt.url {
                openURL(url)
            }
            if status.chatgpt.status == "waiting" {
                beginPolling()
            } else if status.chatgpt.status == "connected" {
                feedback = "Connected to ChatGPT."
            }
        } catch {
            self.error = "\(error)"
        }
    }

    func chooseModel(_ model: String) async {
        guard !changingModel else { return }
        changingModel = true
        defer { changingModel = false }
        let generation = serverGeneration
        clearBanner()
        do {
            let value: Any = model.isEmpty ? NSNull() : model
            let response = try await sendRequest("PUT", "onboarding/model", ["model": value])
            guard generation == serverGeneration else { return }
            apply(response)
        } catch {
            guard generation == serverGeneration else { return }
            self.error = "Couldn’t change the default model."
        }
    }

    func openPendingChatGPTURL() {
        if let url = status.chatgpt.url {
            openURL(url)
        }
    }

    func cancelChatGPTLogin() async {
        stopPolling()
        busyChatGPT = true
        error = nil
        feedback = nil
        defer { busyChatGPT = false }
        do {
            let raw = try await sendRequest("POST", "onboarding/chatgpt/cancel", nil)
            apply(raw)
        } catch {
            self.error = "\(error)"
        }
    }

    func logoutChatGPT() async {
        stopPolling()
        busyChatGPT = true
        error = nil
        feedback = nil
        defer { busyChatGPT = false }
        do {
            let raw = try await sendRequest("POST", "onboarding/chatgpt/logout", nil)
            apply(raw)
            feedback = "Signed out of ChatGPT."
        } catch {
            self.error = "\(error)"
        }
    }

    func saveKey(_ envName: String) async {
        let trimmed = (keyDrafts[envName] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        savingKey = envName
        error = nil
        feedback = nil
        defer { savingKey = nil }
        do {
            let raw = try await sendRequest("POST", "onboarding/keys", ["keys": [envName: trimmed]])
            keyDrafts[envName] = ""
            apply(raw)
            feedback = "Saved and applied."
        } catch {
            self.error = "\(error)"
        }
    }

    func removeKey(_ envName: String) async {
        savingKey = envName
        error = nil
        feedback = nil
        defer { savingKey = nil }
        do {
            let raw = try await sendRequest("POST", "onboarding/keys", ["keys": [envName: NSNull()]])
            keyDrafts[envName] = ""
            apply(raw)
            feedback = "Key removed."
        } catch {
            self.error = "\(error)"
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    private func beginPolling() {
        stopPolling()
        pollTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(1500))
                guard !Task.isCancelled else { break }
                do {
                    let raw = try await sendRequest("GET", "onboarding", nil)
                    apply(raw)
                    if status.chatgpt.status == "connected" {
                        feedback = "Connected to ChatGPT."
                        break
                    }
                    if status.chatgpt.status == "error" {
                        if let detail = status.chatgpt.error, !detail.isEmpty {
                            error = detail
                        }
                        break
                    }
                    if status.chatgpt.status != "waiting" {
                        break
                    }
                } catch {
                    self.error = "\(error)"
                    break
                }
            }
            pollTask = nil
        }
    }

    private func apply(_ raw: [String: Any]) {
        status = OnboardingStatus.parse(raw)
        syncNeedsOnboarding(status.needsOnboarding)
        if !initializedExpansion {
            initializedExpansion = true
            if !status.chatgpt.connected && status.modelKeys.contains(where: \.configured) {
                apiKeysExpanded = true
            }
        }
        if let cgError = status.chatgpt.error, !cgError.isEmpty {
            error = cgError
        }
    }
}

@MainActor @Observable
final class OnboardingWindow: NSObject, NSWindowDelegate {
    static let shared = OnboardingWindow()

    @ObservationIgnored private var window: NSWindow?

    func show() {
        let window = self.window ?? make()
        self.window = window
        Task { await OnboardingStore.shared.refresh() }
        NSApp.activate()
        window.center()
        window.makeKeyAndOrderFront(nil)
    }

    func close() {
        OnboardingStore.shared.stopPolling()
        window?.close()
    }

    func windowWillClose(_ notification: Notification) {
        OnboardingStore.shared.stopPolling()
    }

    private func make() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 500, height: 365),
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Add a model"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(Paper.canvas)
        window.delegate = self
        window.contentView = NSHostingView(
            rootView: AddModelView(store: OnboardingStore.shared, close: close)
        )
        return window
    }
}

private struct AddModelView: View {
    @Bindable var store: OnboardingStore
    let close: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Add a model")
                .font(Paper.Typography.h2)
                .padding(.horizontal, 24)
                .padding(.top, 38)
                .padding(.bottom, 12)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let error = store.error {
                        Text(error)
                            .font(Paper.Typography.callout)
                            .foregroundStyle(Paper.errorInk)
                    }
                    ModelSetupPanel(store: store)
                }
                .padding(.horizontal, 24)
                .padding(.bottom, 12)
            }
            Divider()
            HStack {
                Spacer()
                Button(store.status.hasModel ? "Done" : "Close", action: close)
                    .keyboardShortcut(.cancelAction)
            }
            .padding(16)
        }
        .frame(width: 500, height: 365)
        .background(Paper.canvas)
    }
}

struct PaperFieldBox<Content: View>: View {
    let focused: Bool
    @ViewBuilder let content: Content

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
        content
            .textFieldStyle(.plain)
            .font(Paper.Typography.callout)
            .foregroundStyle(Paper.inkPrimary)
            .padding(.horizontal, 10)
            .padding(.vertical, 6.5)
            .background(Paper.canvas, in: shape)
            .overlay(
                shape.strokeBorder(
                    focused ? Paper.borderFocus : Paper.borderStrong,
                    lineWidth: focused ? 1 : 0.5
                )
            )
            .animation(.easeOut(duration: 0.12), value: focused)
    }
}

struct ModelSetupPanel: View {
    @Bindable var store: OnboardingStore

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text("Default model")
                Spacer()
                if store.status.modelLocked || store.status.models.isEmpty {
                    Text(store.status.activeModel.map(modelLabel) ?? "Not set")
                        .foregroundStyle(Paper.inkSecondary)
                } else {
                    Picker("Default model", selection: Binding(
                        get: { store.status.defaultModel ?? "" },
                        set: { model in Task { await store.chooseModel(model) } }
                    )) {
                        Text(store.status.defaultModel == nil
                             ? "Automatic (\(store.status.activeModel.map(modelLabel) ?? "Not set"))"
                             : "Automatic").tag("")
                        ForEach(store.status.models, id: \.self) { model in
                            Text(modelLabel(model)).tag(model)
                        }
                    }
                    .labelsHidden()
                    .fixedSize()
                    .disabled(store.changingModel)
                }
            }
            .font(Paper.Typography.body)
            Divider()
            chatGPTRow
            if !store.status.modelKeys.isEmpty {
                Divider()
                DisclosureGroup("Use an API key", isExpanded: $store.apiKeysExpanded) {
                    ForEach(store.status.modelKeys) { key in
                        KeyRowView(key: key, store: store)
                    }
                }
                .font(Paper.Typography.body)
            }
        }
    }

    private func modelLabel(_ id: String) -> String {
        let parts = id.split(separator: ":", maxSplits: 1)
        guard parts.count == 2 else { return id }
        return parts[0] == "openai-chatgpt" ? "\(parts[1]) (ChatGPT)" : String(parts[1])
    }

    private var chatGPTRow: some View {
        let cg = store.status.chatgpt
        return HStack(spacing: 10) {
            Text(cg.status == "waiting" ? "Waiting for sign-in…" : "ChatGPT")
                .font(Paper.Typography.body)
            Spacer()
            if store.busyChatGPT || cg.status == "waiting" {
                ProgressView().controlSize(.small)
            }
            if cg.status == "waiting" {
                Button("Open sign-in page", action: store.openPendingChatGPTURL)
                    .disabled(cg.url == nil)
                Button("Cancel") { Task { await store.cancelChatGPTLogin() } }
                    .disabled(store.busyChatGPT)
            } else if cg.connected {
                Text("Connected").foregroundStyle(Paper.inkSecondary)
                Button("Sign out") { Task { await store.logoutChatGPT() } }
                    .disabled(store.busyChatGPT)
            } else if URL(string: BudSettings.load().url)?.scheme != "https" {
                Button("Sign in with ChatGPT") { Task { await store.startChatGPTLogin() } }
                    .disabled(store.busyChatGPT || store.status == .empty)
            } else {
                Text("Use an API key for this server").foregroundStyle(Paper.inkSecondary)
            }
        }
    }
}

struct IntegrationKeysPanel: View {
    @Bindable var store: OnboardingStore

    var body: some View {
        VStack(spacing: 0) {
            ForEach(store.status.integrationKeys) { key in
                if key.id != store.status.integrationKeys.first?.id { Divider() }
                KeyRowView(key: key, store: store)
            }
        }
    }
}

private struct KeyRowView: View {
    let key: ManagedKeyStatus
    @Bindable var store: OnboardingStore
    @FocusState private var focused: Bool

    private var draftBinding: Binding<String> {
        Binding(
            get: { store.keyDrafts[key.envName] ?? "" },
            set: { store.keyDrafts[key.envName] = $0 }
        )
    }

    private var hasDraft: Bool {
        !(store.keyDrafts[key.envName] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var busy: Bool {
        store.savingKey == key.envName
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(key.label)
                        .font(Paper.Typography.label)
                        .foregroundStyle(Paper.inkPrimary)
                    if !key.description.isEmpty {
                        Text(key.description)
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkSecondary)
                    }
                }
                Spacer(minLength: 8)
                if key.configured {
                    Text("Key saved")
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkSecondary)
                }
            }

            HStack(spacing: 8) {
                PaperFieldBox(focused: focused) {
                    SecureField(key.configured ? "Replace API key" : "API key", text: draftBinding)
                    .accessibilityLabel("\(key.label) API key")
                    .focused($focused)
                    .onSubmit {
                        if hasDraft && !busy {
                            Task { await store.saveKey(key.envName) }
                        }
                    }
                }

                if busy {
                    ProgressView().controlSize(.small)
                }

                Button("Save") { Task { await store.saveKey(key.envName) } }
                    .buttonStyle(CardButton(kind: hasDraft ? .primary : .secondary))
                    .disabled(!hasDraft || busy)

                if key.configured {
                    Button("Remove") { Task { await store.removeKey(key.envName) } }
                        .buttonStyle(CardButton(kind: .quiet))
                        .disabled(busy)
                }
            }
        }
        .padding(.vertical, 12)
    }
}

struct StatusStrip: View {
    let text: String
    let isError: Bool
    var onDismiss: (() -> Void)? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: isError ? "exclamationmark.triangle.fill" : "checkmark.circle.fill")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(isError ? Paper.errorInk : Paper.approveInk)
            Text(text)
                .font(Paper.Typography.callout)
                .foregroundStyle(isError ? Paper.errorInk : Paper.approveInk)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
            if let onDismiss {
                Button(action: onDismiss) {
                    Image(systemName: "xmark")
                        .font(.system(size: 9, weight: .semibold))
                }
                .buttonStyle(IconButton(size: 18))
                .accessibilityLabel("Dismiss")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
                .fill(isError ? Paper.denyFill : Paper.approveFill)
        )
    }
}
