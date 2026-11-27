import AppKit
import Testing
@testable import BudCompanion

@MainActor
struct PetSpriteTests {
    @Test
    func optionalEntriesFallBackIndependently() throws {
        for key in ["actions", "states"] {
            for invalid in [NSNull(), 42, "invalid"] as [Any] {
                var manifest = Self.manifest()
                var entries = manifest[key] as! [String: Any]
                entries["failed"] = invalid
                manifest[key] = entries
                let sprite = try load(manifest)
                let start = Date(timeIntervalSince1970: 1000)
                #expect(sprite.frame(for: PetPose(status: .failed, statusSince: start), at: start)
                        == sprite.frame(for: PetPose(status: .idle, statusSince: start), at: start))
            }
        }
    }

    @Test
    func oneShotAndReactionTiming() throws {
        let sprite = try load(Self.manifest())
        let start = Date(timeIntervalSince1970: 1000)
        let first = CGRect(x: 2, y: 0, width: 2, height: 2)
        let last = CGRect(x: 4, y: 0, width: 2, height: 2)
        var pose = PetPose(status: .done, statusSince: start)
        #expect(sprite.frame(for: pose, at: start) == first)
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.15)) == last)
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(10)) == last)
        pose.reaction = (.hop, start)
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.21)) == first)
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.35)) == last)
        pose.reaction = (.hop, start.addingTimeInterval(0.05))
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.26)) == last,
                "A reaction during a status must not restart that status.")
        pose.reaction = (.hop, start.addingTimeInterval(1))
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(1.21)) == last)
    }

    @Test
    func replacementReactionPreservesDeferredStart() throws {
        var manifest = Self.manifest()
        var actions = manifest["actions"] as! [String: Any]
        actions["done"] = [
            "loop": false,
            "frames": [2, 4, 6, 8].map { ["rect": [$0, 0, 2, 2], "durationMs": 100] },
        ]
        manifest["actions"] = actions
        let sprite = try load(manifest)
        let start = Date(timeIntervalSince1970: 1000)
        var pose = PetPose(status: .done, statusSince: start.addingTimeInterval(0.05),
                           reaction: (.hop, start), reactionAtStatusStart: (.hop, start))
        let beforeReplacement = sprite.frame(for: pose, at: start.addingTimeInterval(0.46))
        pose.reaction = (.hop, start.addingTimeInterval(0.22))
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.46)) == beforeReplacement)
        #expect(beforeReplacement == CGRect(x: 6, y: 0, width: 2, height: 2))
    }

    @Test
    func runFinishPlaysOnceAndHoldsTheFinalFrameForThreeHundredMilliseconds() throws {
        var manifest = Self.manifest()
        var actions = manifest["actions"] as! [String: Any]
        actions["done"] = [
            "loop": true,
            "frames": [2, 4].map { ["rect": [$0, 0, 2, 2], "durationMs": 100] },
        ]
        manifest["actions"] = actions
        let sprite = try load(manifest)
        let start = Date(timeIntervalSince1970: 1000)
        let pose = PetPose(status: .done, statusSince: start, reaction: (.hop, start), isRunFinish: true)

        #expect(sprite.finishDuration(reduceMotion: false) == .milliseconds(400))
        #expect(sprite.frame(for: pose, at: start)?.minX == 2)
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.101))?.minX == 4)
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.399))?.minX == 4)
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.401))?.minX == 0)

        #expect(sprite.finishDuration(reduceMotion: true) == .milliseconds(300))
        #expect(sprite.frame(for: pose, at: start, reduceMotion: true)?.minX == 4)
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.299), reduceMotion: true)?.minX == 4)
        #expect(sprite.frame(for: pose, at: start.addingTimeInterval(0.301), reduceMotion: true)?.minX == 0)
    }

    @Test
    func aNewRunInterruptsTheFinishGesture() throws {
        let companion = Companion()
        companion.handle(try #require(BudEvent(json: #"{"seq":1,"type":"run.completed","data":{}}"#)))
        #expect(companion.pose.isRunFinish)
        #expect(companion.pose.reaction == nil)

        companion.handle(try #require(BudEvent(json: #"{"seq":2,"type":"run.started","data":{}}"#)))
        #expect(companion.pose.status == .working)
        #expect(!companion.pose.isRunFinish)
    }

    @Test(arguments: ["send_email", "ask_user"])
    func aDecisionInterruptsTheFinishGestureAndItDoesNotResume(tool: String) throws {
        let companion = Companion()
        companion.handle(try #require(BudEvent(json: #"{"seq":1,"type":"run.completed","data":{}}"#)))
        #expect(companion.pose.isRunFinish)

        companion.handle(try #require(BudEvent(json:
            #"{"seq":2,"type":"approval.requested","data":{"approval_id":"a","tool":"\#(tool)"}}"#)))
        #expect(companion.pose.status == .needsYou)
        #expect(!companion.pose.isRunFinish)

        companion.handle(try #require(BudEvent(json:
            #"{"seq":3,"type":"approval.denied","data":{"approval_id":"a"}}"#)))
        #expect(companion.pose.status == .idle)
        #expect(!companion.pose.isRunFinish)
    }

    @Test
    func priorityAndFixedPanelBox() throws {
        let sprite = try load(Self.manifest())
        let start = Date(timeIntervalSince1970: 1000)
        var pose = PetPose(status: .done, statusSince: start,
                           walk: (.walkLeft, start), listeningSince: start, reaction: (.hop, start))
        #expect(sprite.frame(for: pose, at: start)?.minX == 6)
        pose.walk = nil
        #expect(sprite.frame(for: pose, at: start)?.minX == 8)
        pose.listeningSince = nil
        #expect(sprite.frame(for: pose, at: start)?.minX == 10)
        #expect(sprite.displaySize == PetPanel.pet,
                "Arbitrary source cell sizes must keep the panel's fixed pet box.")
    }

    @Test
    func malformedIdleIsRejected() throws {
        var manifest = Self.manifest()
        manifest["actions"] = ["idle": NSNull()]
        #expect(try withFixture(manifest) { PetSprite.loadManifest($0) } == nil)
    }

    private static func manifest() -> [String: Any] {
        func action(_ xs: [Int], loop: Bool = true) -> [String: Any] {
            ["loop": loop, "frames": xs.map { ["rect": [$0, 0, 2, 2], "durationMs": 100] }]
        }
        return [
            "version": 1, "atlas": "atlas.png", "cell": [2, 2],
            "states": ["idle": "idle", "done": "done", "failed": "failed"],
            "actions": [
                "idle": action([0]), "done": action([2, 4], loop: false),
                "failed": action([2, 4], loop: false), "walk-left": action([6]),
                "listen": action([8]), "hop": action([10, 12], loop: false),
            ],
        ]
    }

    private func load(_ manifest: [String: Any]) throws -> PetSprite {
        try withFixture(manifest) { try #require(PetSprite.loadManifest($0)) }
    }

    private func withFixture<T>(_ manifest: [String: Any], _ body: (URL) throws -> T) throws -> T {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let context = try #require(CGContext(
            data: nil, width: 16, height: 2, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        let image = NSBitmapImageRep(cgImage: try #require(context.makeImage()))
        try #require(image.representation(using: .png, properties: [:])).write(to: folder.appendingPathComponent("atlas.png"))
        try JSONSerialization.data(withJSONObject: manifest).write(to: folder.appendingPathComponent("manifest.json"))
        return try body(folder)
    }
}
