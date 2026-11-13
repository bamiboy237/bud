import AppKit
import SwiftUI

/// The pet's window. SwiftUI can't yet make a window that floats over every app without taking
/// focus when clicked, so this one AppKit piece hosts the SwiftUI views:
/// - `.nonactivatingPanel`: clicking the pet never pulls focus from the app you're in;
/// - `canBecomeKey`: the input field can still take typing, like Spotlight;
/// - every Space and over full-screen apps, at floating level, remembering where you drag it.
final class PetPanel: NSPanel {
    static let size = NSSize(width: 380, height: 320)

    init(root: some View) {
        super.init(
            contentRect: NSRect(origin: .zero, size: Self.size),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        hidesOnDeactivate = false
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false
        isMovableByWindowBackground = true
        // Fully transparent pixels pass clicks through to the app underneath.
        contentView = NSHostingView(rootView: root)
        if !setFrameUsingName("BudPet") { placeBottomRight() }
        setFrameAutosaveName("BudPet")
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    private func placeBottomRight() {
        guard let screen = NSScreen.main?.visibleFrame else { return }
        setFrameOrigin(NSPoint(x: screen.maxX - Self.size.width - 24, y: screen.minY + 24))
    }
}
