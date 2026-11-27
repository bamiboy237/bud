import AppKit
import SwiftUI

/// Which side of the pet the bubble opens on. It opens above and to the left, toward the
/// middle of the screen, until the pet is moved near the top or the left edge.
struct Anchor: Equatable {
    var below = false
    var left = false
    var contentWidth = PetPanelGeometry.contentWidth
    var contentHeight = PetPanelGeometry.maxContentHeight
}

enum PetPanelGeometry {
    static let panelSize = CGSize(width: 380, height: 740)
    static let petSize = CGSize(width: 96, height: 104)
    static let panelInset: CGFloat = 12
    static let screenInset: CGFloat = 8
    static let contentWidth: CGFloat = 332
    static let maxContentHeight = panelSize.height - petSize.height - 2 * panelInset

    struct Display {
        let id: UInt32
        let frame: CGRect
        let visibleFrame: CGRect

        var usable: CGRect { visibleFrame.insetBy(dx: screenInset, dy: screenInset) }

        func origin(at fraction: CGPoint) -> CGPoint {
            CGPoint(x: usable.minX + fraction.x * max(0, usable.width - petSize.width),
                    y: usable.minY + fraction.y * max(0, usable.height - petSize.height))
        }

        func fraction(at origin: CGPoint) -> CGPoint {
            CGPoint(x: (origin.x - usable.minX) / max(1, usable.width - petSize.width),
                    y: (origin.y - usable.minY) / max(1, usable.height - petSize.height))
        }

        func containsPet(at origin: CGPoint) -> Bool {
            frame.contains(CGPoint(x: origin.x + petSize.width / 2, y: origin.y + petSize.height / 2))
        }
    }

    struct Positions: Codable {
        var lastDisplayID: UInt32?
        var fractions: [UInt32: CGPoint] = [:]

        func restore(
            on displays: [Display], activeID: UInt32?, legacyOrigin: CGPoint? = nil
        ) -> (display: Display, origin: CGPoint)? {
            if let display = displays.first(where: { $0.id == lastDisplayID }),
               let fraction = fractions[display.id] {
                return (display, display.origin(at: fraction))
            }
            if let legacyOrigin, let display = displays.first(where: { $0.containsPet(at: legacyOrigin) }) {
                return (display, legacyOrigin)
            }
            guard let display = displays.first(where: { $0.id == activeID }) ?? displays.first else { return nil }
            if let fraction = fractions[display.id] ?? lastDisplayID.flatMap({ fractions[$0] }) {
                return (display, display.origin(at: fraction))
            }
            return (display, CGPoint(x: display.visibleFrame.maxX - 36 - petSize.width,
                                     y: display.visibleFrame.minY + 36))
        }
    }

    static func place(
        _ proposed: CGPoint, on display: Display, previous: Anchor
    ) -> (origin: CGPoint, anchor: Anchor, frame: CGRect) {
        let usable = display.usable
        let width = min(contentWidth, usable.width)
        var x = min(max(proposed.x, usable.minX), max(usable.minX, usable.maxX - petSize.width))
        let y = min(max(proposed.y, usable.minY), max(usable.minY, usable.maxY - petSize.height))

        let reach = max(0, width - petSize.width)
        let leftRoom = x - usable.minX
        let rightRoom = usable.maxX - x - petSize.width
        // Keep the current side while it fits, with extra room before returning to the left.
        let opensRight: Bool
        if leftRoom < reach && rightRoom < reach {
            opensRight = rightRoom > leftRoom || (rightRoom == leftRoom && previous.left)
        } else if previous.left {
            opensRight = rightRoom >= reach && leftRoom < reach + 40
        } else {
            opensRight = leftRoom < reach
        }
        if opensRight {
            x = min(x, usable.maxX - max(width, petSize.width))
        } else {
            x = max(x, usable.minX + reach)
        }

        let above = usable.maxY - y - petSize.height
        let below = y - usable.minY
        let opensBelow = previous.below
            ? above < 340 && (below >= 300 || below > above)
            : above < 300 && below > above
        let height = min(maxContentHeight, max(0, opensBelow ? below : above))
        let anchor = Anchor(below: opensBelow, left: opensRight, contentWidth: width, contentHeight: height)
        let frame = CGRect(
            x: opensRight ? x - panelInset : x + petSize.width + panelInset - panelSize.width,
            y: opensBelow ? y + petSize.height + panelInset - panelSize.height : y - panelInset,
            width: panelSize.width, height: panelSize.height)
        return (CGPoint(x: x, y: y), anchor, frame)
    }
}

private extension NSScreen {
    var displayID: UInt32? {
        (deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
    }

    var petDisplay: PetPanelGeometry.Display? {
        displayID.map { .init(id: $0, frame: frame, visibleFrame: visibleFrame) }
    }
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
    static let size = PetPanelGeometry.panelSize
    /// The pet's size and its gap from the panel's edge (the views' padding).
    static let pet = PetPanelGeometry.petSize
    static let inset = PetPanelGeometry.panelInset
    private static let savedKey = "BudPetDisplayPositions"

    /// The pet's bottom-left corner, in screen points.
    private var petOrigin = NSPoint.zero
    private var anchor = Anchor()
    private var positions = PetPanelGeometry.Positions()
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
        let defaults = UserDefaults.standard
        if let data = defaults.data(forKey: Self.savedKey),
           let saved = try? JSONDecoder().decode(PetPanelGeometry.Positions.self, from: data) {
            positions = saved
        }
        let legacyOrigin = positions.lastDisplayID == nil
            ? defaults.string(forKey: "BudPetOrigin").map(NSPointFromString) : nil
        restore(legacyOrigin: legacyOrigin)
        NotificationCenter.default.addObserver(
            self, selector: #selector(screenParametersChanged(_:)),
            name: NSApplication.didChangeScreenParametersNotification, object: NSApp)
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
        let proposed = NSPoint(x: start.pet.x + mouse.x - start.mouse.x, y: start.pet.y + mouse.y - start.mouse.y)
        let displays = NSScreen.screens.compactMap(\.petDisplay)
        guard let display = displays.first(where: { $0.frame.contains(mouse) })
            ?? displays.first(where: { $0.containsPet(at: proposed) })
            ?? displays.first(where: { $0.id == positions.lastDisplayID })
            ?? displays.first else { return }
        place(pet: proposed, on: display)
        if ended { save(on: display) }
    }

    /// Puts the pet at `pet` (kept fully on screen), picks the bubble's side, and moves the
    /// window so the pet lands exactly there.
    private func place(pet: NSPoint, on display: PetPanelGeometry.Display) {
        let placement = PetPanelGeometry.place(pet, on: display, previous: anchor)
        petOrigin = placement.origin
        if placement.anchor != anchor {
            anchor = placement.anchor
            onAnchor(anchor)
        }
        setFrame(placement.frame, display: true)
    }

    @objc private func screenParametersChanged(_ notification: Notification) {
        dragStart = nil
        restore()
    }

    private func restore(legacyOrigin: CGPoint? = nil) {
        guard let placement = positions.restore(
            on: NSScreen.screens.compactMap(\.petDisplay),
            activeID: NSScreen.main?.displayID, legacyOrigin: legacyOrigin) else { return }
        place(pet: placement.origin, on: placement.display)
        if positions.lastDisplayID != placement.display.id { save(on: placement.display) }
    }

    private func save(on display: PetPanelGeometry.Display) {
        positions.lastDisplayID = display.id
        positions.fractions[display.id] = display.fraction(at: petOrigin)
        if let data = try? JSONEncoder().encode(positions) {
            UserDefaults.standard.set(data, forKey: Self.savedKey)
        }
    }
}
