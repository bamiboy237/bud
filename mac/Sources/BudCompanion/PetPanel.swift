import AppKit
import SwiftUI

/// Which side of the pet the bubble opens on. It opens above and to the left, toward the
/// middle of the screen, until the pet is moved near the top or the left edge.
struct Anchor: Equatable {
    var below = false
    var left = false
}

/// The pet's window. SwiftUI can't yet make a window that floats over every app without taking
/// focus when clicked, so this one AppKit piece hosts the SwiftUI views:
/// - `.nonactivatingPanel`: clicking the pet never pulls focus from the app you're in;
/// - `canBecomeKey`: the input field can still take typing, like Spotlight;
/// - every Space and over full-screen apps, at floating level.
///
/// The pet is what you place, not the window. The window is arranged around the pet, with
/// room for the bubble on whichever side has space.
final class PetPanel: NSPanel {
    /// Tall enough for a full bubble, an approval card and the input beside the pet, so nothing
    /// is ever cut off. The empty part is transparent, and clicks there reach the app below.
    static let size = NSSize(width: 380, height: 600)
    /// The pet's size and its gap from the panel's edge (the views' padding).
    static let pet = NSSize(width: 96, height: 104)
    static let inset: CGFloat = 12
    /// Room the bubble needs beside the pet before it switches sides; the gap between the
    /// two thresholds stops it flickering when the pet sits right at the line.
    private static let room = (flip: CGFloat(300), back: CGFloat(340))
    private static let savedKey = "BudPetOrigin"

    /// The pet's bottom-left corner, in screen points.
    private var petOrigin = NSPoint.zero
    private var anchor = Anchor()
    private let onAnchor: (Anchor) -> Void

    init(root: some View, onAnchor: @escaping (Anchor) -> Void) {
        self.onAnchor = onAnchor
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
        // Fully transparent pixels pass clicks through to the app underneath.
        let host = NSHostingView(rootView: root)
        // The panel's size is fixed here, not by its content.
        host.sizingOptions = []
        contentView = host
        let saved = UserDefaults.standard.string(forKey: Self.savedKey).map(NSPointFromString)
        place(pet: saved ?? defaultPetOrigin())
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    /// macOS keeps a window's top below the menu bar. With the bubble's room above the pet,
    /// that would stop the pet halfway up the screen; `place` keeps the pet itself on screen.
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
        frameRect
    }

    /// The open panel, if there is one. The pet drags it.
    @MainActor static var current: PetPanel? {
        NSApp.windows.lazy.compactMap { $0 as? PetPanel }.first
    }

    /// Where a drag began: the pet and the pointer, both in screen points. Measured on screen,
    /// not in the view, because the view moves with the panel as it's dragged.
    private var dragStart: (pet: NSPoint, mouse: NSPoint)?

    func drag(ended: Bool) {
        let mouse = NSEvent.mouseLocation
        let start = dragStart ?? (petOrigin, mouse)
        dragStart = ended ? nil : start
        place(pet: NSPoint(x: start.pet.x + mouse.x - start.mouse.x, y: start.pet.y + mouse.y - start.mouse.y))
        if ended { UserDefaults.standard.set(NSStringFromPoint(petOrigin), forKey: Self.savedKey) }
    }

    /// Puts the pet at `pet` (kept fully on screen), picks the bubble's side, and moves the
    /// window so the pet lands exactly there.
    private func place(pet: NSPoint) {
        guard let visible = screenUnder(pet)?.visibleFrame else { return }
        let x = min(max(pet.x, visible.minX), visible.maxX - Self.pet.width)
        let y = min(max(pet.y, visible.minY), visible.maxY - Self.pet.height)
        petOrigin = NSPoint(x: x, y: y)

        var next = anchor
        let above = visible.maxY - (y + Self.pet.height)
        if anchor.below ? above > Self.room.back : above < Self.room.flip { next.below.toggle() }
        // The bubble is wider than the pet by this much, on the side it extends to.
        let reach = 320 - Self.pet.width
        let leftRoom = x - visible.minX
        if anchor.left ? leftRoom > reach + 40 : leftRoom < reach { next.left.toggle() }
        if next != anchor {
            anchor = next
            onAnchor(next)
        }

        let size = Self.size
        let originX = next.left ? x - Self.inset : x + Self.pet.width + Self.inset - size.width
        let originY = next.below ? y + Self.pet.height + Self.inset - size.height : y - Self.inset
        setFrame(NSRect(x: originX, y: originY, width: size.width, height: size.height), display: true)
    }

    private func defaultPetOrigin() -> NSPoint {
        guard let visible = NSScreen.main?.visibleFrame else { return .zero }
        return NSPoint(x: visible.maxX - 24 - Self.inset - Self.pet.width, y: visible.minY + 24 + Self.inset)
    }

    /// The screen the pet is on, so it can be dragged onto a second display.
    private func screenUnder(_ point: NSPoint) -> NSScreen? {
        let center = NSPoint(x: point.x + Self.pet.width / 2, y: point.y + Self.pet.height / 2)
        return NSScreen.screens.first { $0.frame.contains(center) } ?? screen ?? NSScreen.main
    }
}
