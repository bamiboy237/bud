import AppKit
import Foundation
import Observation
import SwiftUI

enum OnboardingStep: Int, CaseIterable, Identifiable, Equatable, Sendable {
    case model = 1
    case integrations = 2

    var id: Int { rawValue }

    var title: String {
        switch self {
        case .model: "Model"
        case .integrations: "Integrations"
        }
    }

    var indexLabel: String {
        switch self {
        case .model: "1 of 2 · Model"
        case .integrations: "2 of 2 · Optional keys"
        }
    }
}

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
    let configPath: String
    let chatgpt: ChatGPTAuthStatus
    let keys: [ManagedKeyStatus]

    static let empty = OnboardingStatus(
        hasModel: false,
        needsOnboarding: false,
        activeModel: nil,
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
    var step: OnboardingStep = .model
    var stepDirection: CGFloat = 1
    var apiKeysExpanded = false
    var keyDrafts: [String: String] = [:]
    var loading = false
    var savingKey: String? = nil
    var busyChatGPT = false
    var feedback: String? = nil
    var error: String? = nil

    @ObservationIgnored private let sendRequest: Transport
    @ObservationIgnored private let openURL: URLOpener
    @ObservationIgnored private let syncNeedsOnboarding: NeedsOnboardingSink
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var initializedExpansion = false

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

    func go(to next: OnboardingStep) {
        guard step != next else { return }
        stepDirection = next.rawValue >= step.rawValue ? 1 : -1
        step = next
    }

    func clearBanner() {
        error = nil
        feedback = nil
    }

    func refresh() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            let raw = try await sendRequest("GET", "onboarding", nil)
            apply(raw)
        } catch {
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
    @ObservationIgnored private var autoShownThisLaunch = false

    func showIfNeeded() {
        guard !autoShownThisLaunch else { return }
        autoShownThisLaunch = true
        show(startingAt: .model)
    }

    func show(startingAt step: OnboardingStep = .model) {
        OnboardingStore.shared.step = step
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
            contentRect: NSRect(x: 0, y: 0, width: 540, height: 490),
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Set up bud"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(Paper.canvas)
        window.delegate = self
        window.contentView = NSHostingView(
            rootView: OnboardingView(store: OnboardingStore.shared, window: self)
        )
        return window
    }
}

struct BudFaceAvatar: View {
    var size: CGFloat = 46

    private var iconImage: NSImage? {
        if let appIcon = Bundle.main.image(forResource: "AppIcon") {
            return appIcon
        }
        if let current = NSApp.applicationIconImage {
            return current
        }
        return PetSprite.named(Companion.shared.petID)?.icon(height: size)
    }

    var body: some View {
        Group {
            if let image = iconImage {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: .fit)
            } else {
                Image(systemName: "pawprint.fill")
                    .font(.system(size: size * 0.45, weight: .semibold))
                    .foregroundStyle(Paper.inkPrimary)
                    .frame(width: size, height: size)
                    .surface(RoundedRectangle(cornerRadius: Paper.radiusMD, style: .continuous), lift: 1)
            }
        }
        .frame(width: size, height: size)
        .shadow(color: .black.opacity(0.12), radius: 4, y: 2)
        .accessibilityHidden(true)
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

struct OnboardingView: View {
    @Bindable var store: OnboardingStore
    let window: OnboardingWindow
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var stepAnimation: Animation {
        reduceMotion ? .easeOut(duration: 0.14) : .easeOut(duration: 0.2)
    }

    private var bannerAnimation: Animation {
        reduceMotion ? .easeOut(duration: 0.14) : .easeOut(duration: 0.18)
    }

    private var bannerKey: String {
        store.error ?? store.feedback ?? ""
    }

    private var stepTransition: AnyTransition {
        if reduceMotion {
            return .opacity
        }
        return .asymmetric(
            insertion: .offset(x: 10 * store.stepDirection).combined(with: .opacity),
            removal: .offset(x: -10 * store.stepDirection).combined(with: .opacity)
        )
    }

    private var bannerTransition: AnyTransition {
        reduceMotion
            ? .opacity
            : .opacity.combined(with: .scale(scale: 0.985, anchor: .top))
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    banner
                    Group {
                        switch store.step {
                        case .model:
                            ModelSetupPanel(store: store, showHero: true)
                        case .integrations:
                            IntegrationKeysPanel(store: store)
                        }
                    }
                    .id(store.step)
                    .transition(stepTransition)
                }
                .padding(.horizontal, 24)
                .padding(.vertical, 20)
            }
            .animation(bannerAnimation, value: bannerKey)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            footer
        }
        .frame(width: 540, height: 490)
        .background(Paper.canvas)
    }

    private var header: some View {
        HStack(spacing: 10) {
            Text("bud")
                .font(Paper.Typography.wordmark)
                .foregroundStyle(Paper.inkPrimary)
            Spacer(minLength: 8)
            wayfindingBar
        }
        .padding(.leading, 74)
        .padding(.trailing, 20)
        .frame(height: 46)
    }

    private var wayfindingBar: some View {
        let pillShape = RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
        let trackShape = RoundedRectangle(cornerRadius: Paper.radiusMD, style: .continuous)
        return HStack(spacing: 3) {
            ForEach(OnboardingStep.allCases) { item in
                let active = store.step == item
                let completed = item == .model && store.status.hasModel
                Button {
                    withAnimation(stepAnimation) {
                        store.go(to: item)
                    }
                } label: {
                    HStack(spacing: 5) {
                        if completed {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(Paper.approveInk)
                        } else {
                            Text("\(item.rawValue)")
                                .font(Paper.Typography.ledgerMeta)
                                .foregroundStyle(active ? Paper.inkPrimary : Paper.inkMuted)
                        }
                        Text(item.title)
                            .font(Paper.Typography.captionMedium)
                            .foregroundStyle(active ? Paper.inkPrimary : Paper.inkMuted)
                    }
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                }
                .buttonStyle(PaperHit(shape: pillShape, fill: active ? Paper.elevated : .clear))
                .overlay {
                    if active {
                        pillShape
                            .strokeBorder(Paper.borderSubtle, lineWidth: 0.5)
                            .allowsHitTesting(false)
                    }
                }
                .accessibilityLabel("Step \(item.rawValue): \(item.title)")
                .accessibilityValue(active ? "Current" : (completed ? "Configured" : ""))
            }
        }
        .padding(3)
        .background(Paper.recessed, in: trackShape)
        .overlay(trackShape.strokeBorder(Paper.hairline, lineWidth: 0.5))
    }

    @ViewBuilder
    private var banner: some View {
        if let err = store.error, !err.isEmpty {
            StatusStrip(text: err, isError: true) { store.clearBanner() }
                .transition(bannerTransition)
        } else if let ok = store.feedback, !ok.isEmpty {
            StatusStrip(text: ok, isError: false) { store.clearBanner() }
                .transition(bannerTransition)
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            if store.step == .model {
                Button("Skip for now") { window.close() }
                    .buttonStyle(CardButton(kind: .quiet))
                Spacer(minLength: 8)
                if !store.status.hasModel {
                    Text("You can set a model later")
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkMuted)
                }
                Button("Continue") {
                    withAnimation(stepAnimation) {
                        store.go(to: .integrations)
                    }
                }
                .buttonStyle(CardButton(kind: store.status.hasModel ? .primary : .secondary))
            } else {
                Button("Back") {
                    withAnimation(stepAnimation) {
                        store.go(to: .model)
                    }
                }
                .buttonStyle(CardButton(kind: .quiet))
                Spacer(minLength: 8)
                Button("Start using bud") {
                    window.close()
                    ConversationWindow.shared.show()
                }
                .buttonStyle(CardButton(kind: .primary))
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .background(Paper.recessed.opacity(0.6))
    }
}

struct ModelSetupPanel: View {
    @Bindable var store: OnboardingStore
    var showHero = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if showHero {
                HStack(alignment: .center, spacing: 14) {
                    BudFaceAvatar(size: 56)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Meet bud")
                            .font(Paper.Typography.h1)
                            .tracking(Paper.Typography.h1Tracking)
                            .foregroundStyle(Paper.inkPrimary)
                        Text("Connect ChatGPT or an API key to get started. Changes apply live.")
                            .font(Paper.Typography.callout)
                            .foregroundStyle(Paper.inkSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .modifier(Stagger(index: 0))
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Models")
                        .font(Paper.Typography.h2)
                        .tracking(Paper.Typography.h2Tracking)
                        .foregroundStyle(Paper.inkPrimary)
                    Text("Use your ChatGPT subscription or paste an API key. Changes apply live without restarting bud.")
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            chatGPTCard
                .modifier(Stagger(index: showHero ? 1 : 0))

            apiKeysCard
                .modifier(Stagger(index: showHero ? 2 : 1))
        }
    }

    private var chatGPTCard: some View {
        let cg = store.status.chatgpt
        let shape = RoundedRectangle(cornerRadius: Paper.radiusLG, style: .continuous)
        return VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("ChatGPT Subscription")
                        .font(Paper.Typography.h3)
                        .tracking(Paper.Typography.h3Tracking)
                        .foregroundStyle(Paper.inkPrimary)
                    if let selected = cg.selectedModel, cg.connected {
                        Text("Active model: openai-chatgpt:\(selected)")
                            .font(Paper.Typography.ledgerMeta)
                            .foregroundStyle(Paper.inkMuted)
                    } else {
                        Text("Use your ChatGPT Plus or Pro plan via browser sign-in.")
                            .font(Paper.Typography.callout)
                            .foregroundStyle(Paper.inkSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 8)
                SetupPill(
                    text: cg.status == "waiting"
                        ? "Waiting..."
                        : (cg.connected ? "Connected" : "Recommended"),
                    active: cg.connected,
                    accent: !cg.connected && cg.status != "waiting"
                )
            }

            HStack(spacing: 8) {
                if cg.status == "waiting" {
                    Button("Open sign-in page") { store.openPendingChatGPTURL() }
                        .buttonStyle(CardButton(kind: .secondary))
                        .disabled(cg.url == nil)
                    Button("Cancel") { Task { await store.cancelChatGPTLogin() } }
                        .buttonStyle(CardButton(kind: .quiet))
                        .disabled(store.busyChatGPT)
                    Spacer(minLength: 8)
                    ProgressView().controlSize(.small)
                    Text("Waiting for browser sign-in...")
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkMuted)
                } else if cg.connected {
                    Button("Sign out") { Task { await store.logoutChatGPT() } }
                        .buttonStyle(CardButton(kind: .secondary))
                        .disabled(store.busyChatGPT)
                    Spacer(minLength: 8)
                    if store.busyChatGPT {
                        ProgressView().controlSize(.small)
                    }
                } else {
                    Button("Sign in with ChatGPT") { Task { await store.startChatGPTLogin() } }
                        .buttonStyle(CardButton(kind: .primary))
                        .disabled(store.busyChatGPT)
                    Spacer(minLength: 8)
                    if store.busyChatGPT {
                        ProgressView().controlSize(.small)
                    }
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .surface(shape, lift: 1, fill: Paper.elevated, border: Paper.borderStrong, borderWidth: 0.5)
        .animation(.easeOut(duration: 0.18), value: cg.status)
    }

    private var apiKeysCard: some View {
        let shape = RoundedRectangle(cornerRadius: Paper.radiusLG, style: .continuous)
        let configuredCount = store.status.modelKeys.filter(\.configured).count
        let toggleAnimation: Animation = reduceMotion ? .easeOut(duration: 0.14) : .easeOut(duration: 0.2)
        let revealTransition: AnyTransition = reduceMotion
            ? .opacity
            : .opacity.combined(with: .scale(scale: 0.985, anchor: .top))

        return VStack(spacing: 0) {
            Button {
                withAnimation(toggleAnimation) {
                    store.apiKeysExpanded.toggle()
                }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Paper.inkMuted)
                        .rotationEffect(.degrees(store.apiKeysExpanded ? 90 : 0))
                        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: store.apiKeysExpanded)
                        .frame(width: 12)
                    Text("Or use an API key")
                        .font(Paper.Typography.label)
                        .foregroundStyle(Paper.inkPrimary)
                    Spacer(minLength: 8)
                    if configuredCount > 0 {
                        SetupPill(text: "\(configuredCount) configured", active: true)
                    } else {
                        Text("OpenAI, DeepSeek, Mistral, Meta")
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkMuted)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(PaperHit(shape: Rectangle(), fill: .clear))

            if store.apiKeysExpanded {
                Rectangle().fill(Paper.hairline).frame(height: 0.5)
                VStack(spacing: 0) {
                    let items = store.status.modelKeys
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, key in
                        if index > 0 {
                            Rectangle().fill(Paper.hairline).frame(height: 0.5)
                        }
                        KeyRowView(key: key, store: store)
                    }
                    if items.isEmpty {
                        Text(store.loading ? "Loading providers..." : "No API key providers listed yet.")
                            .font(Paper.Typography.callout)
                            .foregroundStyle(Paper.inkMuted)
                            .padding(14)
                    }
                }
                .transition(revealTransition)
            }
        }
        .clipShape(shape)
        .surface(shape, lift: 1, fill: Paper.elevated, border: Paper.borderStrong, borderWidth: 0.5)
    }
}

struct IntegrationKeysPanel: View {
    @Bindable var store: OnboardingStore

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Paper.radiusLG, style: .continuous)
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Optional integrations")
                    .font(Paper.Typography.h2)
                    .tracking(Paper.Typography.h2Tracking)
                    .foregroundStyle(Paper.inkPrimary)
                Text("Both keys are optional. Saving takes effect immediately without restarting bud.")
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(spacing: 0) {
                let items = store.status.integrationKeys
                ForEach(Array(items.enumerated()), id: \.element.id) { index, key in
                    if index > 0 {
                        Rectangle().fill(Paper.hairline).frame(height: 0.5)
                    }
                    KeyRowView(key: key, store: store)
                }
                if items.isEmpty {
                    Text(store.loading ? "Loading integrations..." : "No integration keys listed yet.")
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkMuted)
                        .padding(14)
                }
            }
            .clipShape(shape)
            .surface(shape, lift: 1, fill: Paper.elevated, border: Paper.borderStrong, borderWidth: 0.5)

            Text("After adding Composio, ask bud to check Gmail or Calendar and it will open a sign-in card right in chat.")
                .font(Paper.Typography.callout)
                .foregroundStyle(Paper.inkSecondary)
                .fixedSize(horizontal: false, vertical: true)

            Text("Saved to \(store.status.configPath)")
                .font(Paper.Typography.ledgerMeta)
                .foregroundStyle(Paper.inkMuted)
                .textSelection(.enabled)
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
                SetupPill(text: key.configured ? "Configured" : "Not set", active: key.configured)
            }

            HStack(spacing: 8) {
                PaperFieldBox(focused: focused) {
                    SecureField(
                        key.configured ? "Paste new \(key.envName) to replace" : key.envName,
                        text: draftBinding
                    )
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
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }
}

private struct SetupPill: View {
    let text: String
    let active: Bool
    var accent: Bool = false

    private var ink: Color {
        if active { return Paper.approveInk }
        if accent { return Paper.accent }
        return Paper.inkMuted
    }

    private var fill: Color {
        if active { return Paper.approveFill }
        if accent { return Paper.accentFill }
        return Paper.fillSubtle
    }

    var body: some View {
        Text(text)
            .font(Paper.Typography.ledgerMeta)
            .foregroundStyle(ink)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Capsule(style: .continuous).fill(fill))
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
