import AppKit
import Testing
@testable import BudCompanion

@MainActor
struct PupSpriteTests {
    private var pupFolder: URL {
        URL(filePath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appending(path: "Resources/pets/pup")
    }

    @Test
    func pupAssetsAreStagedAndValid() throws {
        let manifestUrl = pupFolder.appending(path: "pet.json")
        let sheetUrl = pupFolder.appending(path: "spritesheet.png")
        #expect(FileManager.default.fileExists(atPath: manifestUrl.path))
        #expect(FileManager.default.fileExists(atPath: sheetUrl.path))

        let data = try #require(JSONSerialization.jsonObject(
            with: Data(contentsOf: manifestUrl)) as? [String: Any])
        #expect(data["id"] as? String == "pup")
        #expect(data["spritesheetPath"] as? String == "spritesheet.png")

        let sprite = try #require(PetSprite.loadCodex(pupFolder))
        #expect(sprite.cell == CGSize(width: 192, height: 208))

        let start = Date(timeIntervalSince1970: 1000)
        let workingPose = PetPose(status: .working, statusSince: start)
        let workingRect = try #require(sprite.frame(for: workingPose, at: start))
        #expect(workingRect.origin.y == 1456)
        #expect(workingRect.size == CGSize(width: 192, height: 208))
        #expect(sprite.image(workingRect) != nil)

        let donePose = PetPose(status: .done, statusSince: start)
        let doneRect = try #require(sprite.frame(for: donePose, at: start))
        #expect(doneRect.origin.y == 1664)
        #expect(doneRect.size == CGSize(width: 192, height: 208))
        #expect(sprite.image(doneRect) != nil)

        let idlePose = PetPose(status: .idle, statusSince: start)
        let idleRect = try #require(sprite.frame(for: idlePose, at: start))
        #expect(idleRect.origin.y == 0)
        #expect(idleRect.size == CGSize(width: 192, height: 208))
        #expect(sprite.image(idleRect) != nil)
    }

    @Test
    func visiblePupsCapAtFour() {
        let companion = Companion()
        #expect(companion.visiblePups.isEmpty)

        for count in [2, 4, 20] {
            let tasks: [[String: Any]] = (1...count).map {
                ["task_id": $0, "title": "Job \($0)", "status": "running"]
            }
            companion.apply(snapshot: ["last_seq": count, "tasks": tasks])
            #expect(companion.activeTasks.count == count)
            #expect(companion.visiblePups.count == min(count, 4))
        }
    }

    @Test
    func taskAnimationTransitions() {
        let taskRunning = RunningTask(id: 1, title: "Code indexing", status: "running")
        #expect(taskRunning.move == .working)

        let taskQueued = RunningTask(id: 2, title: "Search query", status: "queued")
        #expect(taskQueued.move == .working)

        let taskDone = RunningTask(id: 3, title: "Sync repo", status: "completed")
        #expect(taskDone.move == .done)

        let taskFailed = RunningTask(id: 4, title: "Build project", status: "failed")
        #expect(taskFailed.move == .failed)

        let taskPaused = RunningTask(id: 5, title: "Wait on user", status: "paused")
        #expect(taskPaused.move == .idle)
    }

    @Test
    func childPupViewDefaultSizing() {
        #expect(ChildPupView.defaultSize == CGSize(width: 44, height: 48))
        #expect(ChildPupView.defaultSize.width < PetPanel.pet.width)
        #expect(ChildPupView.defaultSize.height < PetPanel.pet.height)
        #expect(PetPanel.pet.width / ChildPupView.defaultSize.width >= 2.0)
    }
}
