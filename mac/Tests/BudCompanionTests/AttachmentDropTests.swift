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

    @Test
    func acceptsDocumentsButDoesNotReadAnExecutableDroppedIntoChat() throws {
        let files = FileManager.default
        let directory = files.temporaryDirectory.appending(path: UUID().uuidString)
        try files.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: directory) }

        let text = directory.appending(path: "briefing.txt")
        try Data("Handoff notes".utf8).write(to: text)
        #expect(try DraftAttachment.read(text).reference.name == "briefing.txt")

        let executable = directory.appending(path: "bash")
        try Data([0xcf, 0xfa, 0xed, 0xfe]).write(to: executable)
        try files.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executable.path())
        #expect(throws: DraftAttachment.ReadError.unsupportedFile) {
            try DraftAttachment.read(executable)
        }

        let renamedExecutable = directory.appending(path: "bash.txt")
        try files.moveItem(at: executable, to: renamedExecutable)
        #expect(throws: DraftAttachment.ReadError.unsupportedFile) {
            try DraftAttachment.read(renamedExecutable)
        }
    }

    @Test
    func doesNotFollowLinksDroppedIntoChat() throws {
        let files = FileManager.default
        let directory = files.temporaryDirectory.appending(path: UUID().uuidString)
        try files.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: directory) }
        let report = directory.appending(path: "report.md")
        try Data("Report".utf8).write(to: report)
        let link = directory.appending(path: "link.md")
        try files.createSymbolicLink(at: link, withDestinationURL: report)
        #expect(throws: DraftAttachment.ReadError.unsupportedFile) {
            try DraftAttachment.read(link)
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
