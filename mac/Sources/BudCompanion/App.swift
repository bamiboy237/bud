import AppKit
import SwiftUI

@main
struct BudCompanionApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            MenuContent(delegate: delegate)
        } label: {
            // The pet itself, so bud is one character everywhere; the paw when none is installed.
            if let icon = PetSprite.named(Companion.shared.petID)?.icon(height: 18) {
                Image(nsImage: icon)
            } else {
                Image(systemName: "pawprint")
            }
        }
        Settings { SettingsView() }
    }
}

private struct MenuContent: View {
    let delegate: AppDelegate
    @Bindable var companion = Companion.shared
    @Bindable var updater = AppUpdater.shared

    var body: some View {
        Text(status)
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
        SettingsLink { Text("Settings…") }
        Divider()
        Button("Quit bud") { NSApp.terminate(nil) }.keyboardShortcut("q")
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
            ConversationWindow.shared.show()
            return
        }
        if !petVisible { togglePet() }
        Companion.shared.inputOpen = true
        focusPanel()
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
