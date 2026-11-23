import Foundation
import Testing
@testable import BudCompanion

struct AttachmentDropTests {
    @Test
    func importsFinderFileURLsWithoutAcceptingWebLinks() throws {
        let file = URL(filePath: "/tmp/briefing notes.md")
        let link = URL(string: "https://example.com/briefing.md")!

        #expect(try DroppedAttachment(fileURLData: file.dataRepresentation).url == file)
        #expect(throws: CocoaError.self) {
            try DroppedAttachment(fileURLData: link.dataRepresentation)
        }
    }

    @Test @MainActor
    func attachmentsRespectCapacityAndLeaveTheDraftTextIntact() async throws {
        let companion = Companion()
        companion.draft = "Briefing draft"
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let files = try (0...DraftAttachment.maxCount).map { number in
            let file = directory.appending(path: "briefing \(number).md")
            try Data("Sample".utf8).write(to: file)
            return try DroppedAttachment(fileURLData: file.dataRepresentation).url
        }

        #expect(companion.canAddAttachments)
        companion.addAttachments(files)
        #expect(!companion.canAddAttachments)
        let deadline = ContinuousClock.now + .seconds(2)
        while companion.readingAttachments, ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(companion.draftAttachments.map(\.reference.name) ==
                files.prefix(DraftAttachment.maxCount).map(\.lastPathComponent))
        #expect(companion.draft == "Briefing draft")
        #expect(!companion.canAddAttachments)
        companion.removeAttachment(try #require(companion.draftAttachments.first?.id))
        #expect(companion.canAddAttachments)
    }
}
