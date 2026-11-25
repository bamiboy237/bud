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
}
