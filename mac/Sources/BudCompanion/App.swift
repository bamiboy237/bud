import AppKit
import SwiftUI

@main
struct BudCompanionApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        let needsModelRefresh = Companion.shared.link == .connected
            && OnboardingStore.shared.status.activeModel == nil
        MenuBarExtra {
            MenuContent(delegate: delegate)
        } label: {
            MenuBarPet(companion: Companion.shared)
        }
        .onChange(of: needsModelRefresh, initial: true) { _, needsRefresh in
            guard needsRefresh else { return }
            Task {
                let store = OnboardingStore.shared
                guard Companion.shared.link == .connected,
                      store.status.activeModel == nil, !store.loading else { return }
                await store.refresh(preservingError: store.error)
            }
        }
        Settings { SettingsView() }
    }
}

@MainActor
struct MenuBarWaiting {
    let waitingCount: Int

    init(companion: Companion) {
        // Questions already have approval cards in the conversation.
        waitingCount = companion.approvals.count + companion.setups.count
    }

    var accessibilityLabel: String {
        switch waitingCount {
        case 0: "Bud"
        case 1: "Bud, 1 item waiting"
        default: "Bud, \(waitingCount) items waiting"
        }
    }

    var reviewTitle: String? {
        switch waitingCount {
        case 0: nil
        case 1: "Review waiting items"
        default: "Review waiting items (\(waitingCount))"
        }
    }

    func menuIcon(_ icon: NSImage) -> NSImage {
        guard waitingCount > 0 else {
            icon.accessibilityDescription = accessibilityLabel
            return icon
        }

        let size = icon.size
        let diameter: CGFloat = 4
        let image = NSImage(
            size: NSSize(width: size.width + diameter + 1, height: size.height), flipped: false
        ) { _ in
            icon.draw(in: NSRect(origin: .zero, size: size))
            NSColor.black.setFill()
            NSBezierPath(ovalIn: NSRect(
                x: size.width + 1, y: size.height - diameter - 1, width: diameter, height: diameter
            )).fill()
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = accessibilityLabel
        return image
    }
}

private struct MenuBarPet: View {
    let companion: Companion

    var body: some View {
        let waiting = MenuBarWaiting(companion: companion)
        let icon = PetSprite.named(companion.petID)?.icon(height: 18)
            ?? NSImage(systemSymbolName: "pawprint", accessibilityDescription: waiting.accessibilityLabel)
        Group {
            if let icon {
                Image(nsImage: waiting.menuIcon(icon))
            } else {
                Image(systemName: "pawprint")
            }
        }
        .accessibilityLabel(waiting.accessibilityLabel)
    }
}

private struct MenuContent: View {
    let delegate: AppDelegate
    @Bindable var companion = Companion.shared
    @Bindable var updater = AppUpdater.shared
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        if let title = MenuBarWaiting(companion: companion).reviewTitle {
            Button(title) { delegate.openConversation() }
            Divider()
        }
        Text(status)
        if companion.link == .connected, let model = OnboardingStore.shared.status.activeModel {
            Text(verbatim: modelLabel(model))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        if companion.needsOnboarding {
            Button("Add a model…") { OnboardingWindow.shared.show() }
        }
        Divider()
        Button("Ask bud…") { delegate.ask() }
            .keyboardShortcut("k")
        Button(delegate.petVisible ? "Hide pet" : "Show pet") { delegate.togglePet() }
        if companion.running { Button("Stop", action: companion.stop) }
        Divider()
        Picker("Steps", selection: $companion.stepsLayout) {
            ForEach(StepsLayout.allCases, id: \.self) { Text($0.title).tag($0) }
        }
        Toggle("Show bud's steps after replies", isOn: $companion.showStepsAfter)
        Divider()
        voice
        Divider()
        Button("Reconnect") { companion.start() }
        Button(companion.diagnosticsCopied ? "Diagnostics copied" : "Copy Diagnostics") {
            companion.copyDiagnostics()
        }
        if !AppUpdater.isDevBuild() {
            updates
        }
        Button("Settings…") {
            openSettings()
            Task { await SettingsTabState.shared.bringSettingsToFront() }
        }
        .keyboardShortcut(",")
        Divider()
        Button("Quit bud") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }

    private func modelLabel(_ id: String) -> String {
        let parts = id.split(separator: ":", maxSplits: 1)
        guard parts.count == 2 else { return id }
        return parts[0] == "openai-chatgpt" ? "\(parts[1]) (ChatGPT)" : String(parts[1])
    }

    @ViewBuilder private var updates: some View {
        switch updater.state {
        case .idle:
            Button("Check for Updates…") { Task { await updater.checkForUpdates() } }
        case .checking:
            Text("Checking for updates…")
        case .upToDate(let current):
            Button("Up to date (v\(current)). Check again") {
                Task { await updater.checkForUpdates() }
            }
        case .available(let version, _):
            Button("Update to v\(version) & Restart") { updater.installUpdate() }
        case .updating(let version):
            Text("Updating to v\(version)… Bud will restart")
        case .error(let message):
            Text(message)
            Button("Check for Updates…") { Task { await updater.checkForUpdates() } }
        }
    }

    /// Hold-to-talk: set it up, watch it download, then a reminder of the key.
    @ViewBuilder private var voice: some View {
        let voice = Voice.shared
        switch voice.state {
        case .off:
            Button("Set up voice…", action: voice.setUp)
        case .preparing(let fraction):
            Text("Setting up voice… \(Int(fraction * 100))%")
        case .failed(let message):
            Text(message)
            Button("Try setting up voice again", action: voice.setUp)
        case .ready, .listening, .transcribing:
            if voice.needsAccessibility {
                // ⌥ Space belongs to another app, and right ⌥ can't be seen without this.
                Button("Allow right ⌥ for talking…", action: voice.allowRightOption)
            } else {
                Text("Hold \(voice.keyName) to talk to bud")
            }
        }
    }

    private var status: String {
        let prefix = AppUpdater.isDevBuild() ? "bud [dev]" : "bud"
        switch companion.link {
        case .connecting: return AppUpdater.isDevBuild() ? "Connecting [dev]…" : "Connecting…"
        case .problem(let detail): return detail
        case .connected:
            if let first = companion.lastFirstText {
                return "\(prefix): \(companion.status.label). First text in \(first.formatted(.units(allowed: [.seconds], fractionalPart: .show(length: 1))))."
            } else {
                return "\(prefix): \(companion.status.label)"
            }
        }
    }
}

@MainActor @Observable
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var panel: PetPanel?
    var petVisible = true

    func applicationDidFinishLaunching(_ notification: Notification) {
        Fonts.register()
        AppearanceStore.shared.applyWindowAppearance()
        let companion = Companion.shared
        let panel = PetPanel(root: PetView(companion: companion) { [weak self] in self?.focusPanel() }) {
            companion.anchor = $0
        }
        panel.orderFrontRegardless()
        self.panel = panel
        BundledBrowse.install()
        companion.start()
        MacActionWorker.shared.start()
        if !AppUpdater.isDevBuild(), !UserDefaults.standard.bool(forKey: "openedBudConversation") {
            UserDefaults.standard.set(true, forKey: "openedBudConversation")
            ConversationWindow.shared.show()
        }
        Voice.shared.start()
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        Companion.shared.refreshSetupStatus()
    }

    func ask() {
        // With the window open, that's where you type.
        if Companion.shared.windowOpen {
            Companion.shared.requestComposerFocus()
            return
        }
        if !petVisible { togglePet() }
        Companion.shared.inputOpen = true
        focusPanel()
    }

    func openConversation() {
        let companion = Companion.shared
        while companion.approvalDeck.canSelectPrevious {
            companion.selectPreviousApproval()
        }
        companion.focus = nil
        let conversation = ConversationWindow.shared
        conversation.show()
        guard let content = NSApp.windows.first(where: { $0.delegate === conversation })?.contentView else {
            return
        }
        // The waiting cards sit at the end of the transcript, even when an open window is scrolled up.
        content.layoutSubtreeIfNeeded()
        guard let scrollView = conversationScrollView(in: content),
              let document = scrollView.documentView else { return }
        let clip = scrollView.contentView
        let bottom = document.isFlipped ? clip.documentRect.maxY - clip.bounds.height : clip.documentRect.minY
        clip.scroll(to: NSPoint(x: clip.bounds.minX, y: bottom))
        scrollView.reflectScrolledClipView(clip)
    }

    private func conversationScrollView(in view: NSView) -> NSScrollView? {
        if let scrollView = view as? NSScrollView { return scrollView }
        return view.subviews.lazy.compactMap { self.conversationScrollView(in: $0) }.first
    }

    func togglePet() {
        petVisible.toggle()
        if petVisible { panel?.orderFrontRegardless() } else { panel?.orderOut(nil) }
    }

    /// Key without activating: typing goes to the field, the app underneath stays frontmost.
    private func focusPanel() {
        panel?.makeKey()
    }
}
