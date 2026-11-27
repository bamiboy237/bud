import Foundation
import Testing
@testable import BudCompanion

@Suite struct MacAppActionTests {
    @Test func notesTextCannotBecomeScriptOrMarkup() throws {
        let directory = URL(filePath: "/fixture/MacActions")
        let command = try #require(MacAppAction.command(
            "notes_create", args: ["<Title>", "Example & <img src=x>"], directory: directory))
        #expect(command.0 == "/usr/bin/osascript")
        #expect(command.1 == [
            "/fixture/MacActions/notes_create.applescript",
            "<div><h1>&lt;Title&gt;</h1></div><div>Example &amp; &lt;img src=x&gt;</div>",
        ])
        #expect(MacAppAction.command("../../../x", args: [], directory: directory) == nil)
        #expect(MacAppAction.command("notes_read", args: ["\0"], directory: directory) == nil)
        #expect(MacAppAction.command("notes_list", args: ["101"], directory: directory) == nil)
    }

    @Test func openingFilesAcceptsOnlySupportedDocumentNames() throws {
        let files = FileManager.default
        let directory = files.temporaryDirectory.appending(path: UUID().uuidString)
        try files.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: directory) }
        try Data(#"{".pdf":"Preview",".docx":""}"#.utf8)
            .write(to: directory.appending(path: "documents.json"))
        #expect(MacAppAction.documentApp("Report.PDF", directory: directory) == "Preview")
        #expect(MacAppAction.documentApp("Draft.docx", directory: directory) == "")
        #expect(MacAppAction.documentApp("../private.pdf", directory: directory) == nil)
        #expect(MacAppAction.documentApp("run.command", directory: directory) == nil)
    }
}
