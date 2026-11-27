import CoreGraphics
import Testing
@testable import BudCompanion

struct PetPanelGeometryTests {
    private typealias Geometry = PetPanelGeometry

    @Test
    func flipsQuickAskBeforeItsFullWidthCrossesEitherInset() {
        let display = display(1, x: -1_200, width: 1_200, height: 800)
        let leftEdge = CGPoint(x: display.usable.minX + 235, y: display.usable.minY)
        let rightEdge = CGPoint(x: display.usable.maxX - 331, y: display.usable.minY)

        let left = Geometry.place(leftEdge, on: display, previous: Anchor())
        let right = Geometry.place(rightEdge, on: display, previous: Anchor(left: true))

        #expect(left.anchor.left)
        #expect(!right.anchor.left)
        #expect(left.anchor.contentWidth == 332)
        #expect(right.anchor.contentWidth == 332)
        expectFits(left, on: display)
        expectFits(right, on: display)
    }

    @Test
    func fitsQuickAskWhenNeitherSideHasRoomAtThePetsCurrentPosition() {
        let display = display(1, x: 0, width: 420, height: 600)
        let pet = CGPoint(x: 160, y: 350)
        let placement = Geometry.place(pet, on: display, previous: Anchor())

        #expect(placement.origin.x != pet.x)
        #expect(placement.anchor.below)
        expectFits(placement, on: display)
    }

    @Test
    func rescuesOntoTheActiveDisplayAndUsesItsSavedPosition() throws {
        let secondary = display(2, x: 1_400, width: 1_900, height: 1_080)
        let active = display(3, x: -1_000, width: 1_000, height: 650)
        let primary = display(1, x: 0, width: 1_400, height: 900)
        let saved = Geometry.Positions(lastDisplayID: secondary.id, fractions: [
            secondary.id: CGPoint(x: 1, y: 0),
            active.id: CGPoint(x: 0, y: 1),
        ])

        let restored = try #require(saved.restore(on: [primary, active], activeID: active.id))
        #expect(restored.display.id == active.id)
        #expect(restored.origin.x == active.usable.minX)
        let placement = Geometry.place(restored.origin, on: restored.display, previous: Anchor())
        #expect(placement.anchor.left)
        #expect(placement.anchor.below)
        expectFits(placement, on: active)
    }

    @Test
    func keepsTheSavedDisplayAndRebasesThePositionAfterItsFrameChanges() throws {
        let original = display(2, x: 1_400, width: 1_900, height: 1_080)
        let resized = display(2, x: -1_280, width: 1_280, height: 700)
        let active = display(1, x: 0, width: 1_400, height: 900)
        let fraction = CGPoint(x: 0.75, y: 0.25)
        let saved = Geometry.Positions(lastDisplayID: original.id, fractions: [
            original.id: original.fraction(at: original.origin(at: fraction)),
        ])

        let restored = try #require(saved.restore(on: [active, resized], activeID: active.id))
        #expect(restored.display.id == resized.id)
        #expect(restored.origin == resized.origin(at: fraction))
        expectFits(Geometry.place(restored.origin, on: resized, previous: Anchor()), on: resized)
    }

    @Test
    func transfersTheRelativePositionWhenTheRemainingDisplayHasNoSavedPosition() throws {
        let remaining = display(1, x: 0, width: 1_280, height: 650)
        let fraction = CGPoint(x: 1, y: 1)
        let saved = Geometry.Positions(lastDisplayID: 2, fractions: [2: fraction])

        let restored = try #require(saved.restore(on: [remaining], activeID: nil))
        #expect(restored.display.id == remaining.id)
        #expect(restored.origin == remaining.origin(at: fraction))
        expectFits(Geometry.place(restored.origin, on: remaining, previous: Anchor()), on: remaining)
    }

    private func display(_ id: UInt32, x: CGFloat, width: CGFloat, height: CGFloat) -> Geometry.Display {
        Geometry.Display(
            id: id, frame: CGRect(x: x, y: 0, width: width, height: height),
            visibleFrame: CGRect(x: x + 12, y: 24, width: width - 12, height: height - 48))
    }

    private func expectFits(
        _ placement: (origin: CGPoint, anchor: Anchor, frame: CGRect), on display: Geometry.Display
    ) {
        let (pet, anchor, frame) = placement
        let x = anchor.left
            ? frame.minX + Geometry.panelInset
            : frame.maxX - Geometry.panelInset - anchor.contentWidth
        let y = anchor.below ? pet.y - anchor.contentHeight : pet.y
        let stack = CGRect(x: x, y: y, width: anchor.contentWidth,
                           height: Geometry.petSize.height + anchor.contentHeight)
        #expect(display.visibleFrame.insetBy(dx: 8, dy: 8).contains(stack))
    }
}
