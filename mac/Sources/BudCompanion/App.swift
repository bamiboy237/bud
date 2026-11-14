import AppKit
import SwiftUI

@main
struct BudCompanionApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra("bud", systemImage: "pawprint") {
            MenuContent(delegate: delegate)
        }
        Settings { SettingsView() }
    }
}

private struct MenuContent: View {
    let delegate: AppDelegate
    @Bindable var companion = Companion.shared

    var body: some View {
        Text(status)
        Divider()
        Button("Ask bud…") { delegate.ask() }
            .keyboardShortcut("k")
        Button(delegate.petVisible ? "Hide pet" : "Show pet") { delegate.togglePet() }
        if companion.status == .working { Button("Stop", action: companion.stop) }
        Button("Reconnect") { companion.start() }
        SettingsLink { Text("Settings…") }
        Divider()
        Button("Quit bud") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }

    private var status: String {
        switch companion.link {
        case .connecting: "Connecting…"
        case .problem(let detail): detail
        case .connected:
            if let first = companion.lastFirstText {
                "bud: \(companion.status.label). First text in \(first.formatted(.units(allowed: [.seconds], fractionalPart: .show(length: 1))))."
            } else {
                "bud: \(companion.status.label)"
            }
        }
    }
}

@MainActor @Observable
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var panel: PetPanel?
    var petVisible = true

    func applicationDidFinishLaunching(_ notification: Notification) {
        let companion = Companion.shared
        let panel = PetPanel(root: PetView(companion: companion) { [weak self] in self?.focusPanel() })
        panel.orderFrontRegardless()
        self.panel = panel
        companion.start()
    }

    func ask() {
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
