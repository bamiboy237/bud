import AppKit
import CryptoKit
import Testing
@testable import BudCompanion

@MainActor
struct BundledPetTests {
    private var folder: URL {
        URL(filePath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appending(path: "Resources/pets/dog")
    }

    private func manifest() throws -> [String: Any] {
        try #require(JSONSerialization.jsonObject(
            with: Data(contentsOf: folder.appending(path: "manifest.json"))) as? [String: Any])
    }

    @Test
    func bundledArtContainsOnlyRuntimeData() throws {
        let data = try manifest()
        #expect(Set(data.keys) == Set(["version", "name", "cell", "atlas", "atlasSha256", "states", "actions"]))
        let name = try #require(data["atlas"] as? String)
        let expected = try #require(data["atlasSha256"] as? String)
        let bytes = try Data(contentsOf: folder.appending(path: name))
        let actual = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        #expect(actual == expected)
        #expect(name == "atlas-\(actual.prefix(12)).png")
        #expect(Set(try FileManager.default.contentsOfDirectory(atPath: folder.path)) == Set(["manifest.json", name]))
        let actions = try #require(data["actions"] as? [String: [String: Any]])
        for action in actions.values {
            #expect(Set(action.keys) == Set(["loop", "frames"]))
            let frames = try #require(action["frames"] as? [[String: Any]])
            for frame in frames {
                #expect(Set(frame.keys) == Set(["rect", "durationMs"]))
            }
        }
    }

    @Test
    func bundledDogLoadsEveryAppPose() throws {
        let data = try manifest()
        let states = try #require(data["states"] as? [String: String])
        #expect(Set(states.keys) == Set(["idle", "working", "needsYou", "done", "failed"]))
        let actions = try #require(data["actions"] as? [String: [String: Any]])
        let sprite = try #require(PetSprite.loadManifest(folder))
        let start = Date(timeIntervalSince1970: 1000)
        let statuses: [(PetStatus, String)] = [
            (.idle, "idle"), (.working, "working"), (.needsYou, "needsYou"), (.done, "done"), (.failed, "failed"),
        ]
        var poses: [(PetPose, String)] = try statuses.map { status, key in
            (PetPose(status: .of(status), statusSince: start), try #require(states[key]))
        }
        poses += [
            (PetPose(status: .idle, statusSince: start, walk: (.walkLeft, start)), "walk-left"),
            (PetPose(status: .idle, statusSince: start, walk: (.walkRight, start)), "walk-right"),
            (PetPose(status: .idle, statusSince: start, listeningSince: start), "listen"),
            (PetPose(status: .idle, statusSince: start, reaction: (.hop, start)), "hop"),
            (PetPose(status: .idle, statusSince: start, reaction: (.wave, start)), "wave"),
        ]
        for (pose, name) in poses {
            let frames = try #require(actions[name]?["frames"] as? [[String: Any]])
            let rect = try #require(frames.first?["rect"] as? [Int])
            try #require(rect.count == 4)
            let expected = CGRect(x: rect[0], y: rect[1], width: rect[2], height: rect[3])
            let actual = try #require(sprite.frame(for: pose, at: start))
            #expect(actual == expected, "\(name) must load instead of falling back to idle.")
            #expect(sprite.image(actual)?.size == PetPanel.pet)
        }
    }
}
