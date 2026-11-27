import Foundation
import Testing
@testable import BudCompanion

@MainActor
struct TextZoomTests {
    @Test
    func zoomClampsAndPersistsAcrossInstances() throws {
        let suite = "BudCompanionTests.TextZoom.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let zoom = TextZoom(defaults: defaults)

        #expect(zoom.delta == 0)
        for _ in 0..<10 { zoom.increase() }
        #expect(zoom.delta == 6)
        #expect(TextZoom(defaults: defaults).delta == 6)

        for _ in 0..<12 { zoom.decrease() }
        #expect(zoom.delta == -3)
        #expect(TextZoom(defaults: defaults).delta == -3)
    }

    @Test
    func resetPersistsTheDefaultSize() throws {
        let suite = "BudCompanionTests.TextZoom.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let zoom = TextZoom(defaults: defaults)

        zoom.increase()
        #expect(zoom.delta == 1)
        zoom.reset()
        #expect(zoom.delta == 0)
        #expect(TextZoom(defaults: defaults).delta == 0)
    }

    @Test
    func proseAndCodeUseTheSamePointDelta() {
        var reply = ReplyType(size: 14, compact: false, zoom: -3)
        #expect(reply.scaledSize == 11)
        #expect(reply.codeSize == 9.5)

        reply.zoom = 6
        #expect(reply.scaledSize == 20)
        #expect(reply.codeSize == 18.5)
    }
}
