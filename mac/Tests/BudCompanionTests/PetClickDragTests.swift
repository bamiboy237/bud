import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct PetClickDragTests {
    @Test
    func aClickImmediatelyAfterDragReleaseIsItsOwnPress() {
        let press = PetPress()
        press.move(.zero)
        press.move(CGSize(width: 4, height: 0))
        #expect(press.release() == .dragging)
        #expect(press.phase == .idle)

        press.move(.zero)
        #expect(press.release() == .pressed)
        #expect(press.phase == .idle)
    }

    @Test
    func aPressStaysADragAfterMovingBackInsideTheThreshold() {
        let press = PetPress()
        press.move(CGSize(width: 2, height: 2))
        #expect(press.phase == .pressed)
        press.move(CGSize(width: 3, height: 3))
        #expect(press.phase == .dragging)
        press.move(.zero)
        #expect(press.release() == .dragging)
    }
}
