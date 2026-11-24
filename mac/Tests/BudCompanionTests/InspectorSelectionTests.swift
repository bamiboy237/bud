import Foundation
import Testing
@testable import BudCompanion

struct InspectorSelectionTests {
    @Test @MainActor
    func reselectingAResourceClosesItEvenWhenItsViewIDChanges() {
        let companion = Companion()
        let connection = BudConnection(base: URL(string: "http://127.0.0.1:8420")!, token: "")
        companion.connectForTesting(connection, session: "chat")
        let file = ChatAttachment(id: "file", name: "plan.md")
        let source = SourcePanel(target: .workspace(path: "plan.md"))

        companion.openAttachment(file)
        #expect(companion.inspector?.target == .attachment(session: "chat", file: file.id))
        companion.openAttachment(file)
        #expect(companion.inspector == nil)
        companion.openRoutines("routine", occurrence: "run")
        #expect(companion.inspector?.target == .routine(session: "chat", routine: "routine", occurrence: "run"))
        companion.openRoutines("routine", occurrence: "run")
        #expect(companion.inspector == nil)
        #expect(companion.toggleInspector(.source(source, request: UUID())))
        #expect(!companion.toggleInspector(.source(source, request: UUID())))
        companion.openBrowserView()
        #expect(companion.inspector?.target == .browser)
        companion.openBrowserView()
        #expect(companion.inspector == nil)
    }

    @Test @MainActor
    func switchingTargetsReplacesTheInspector() {
        let companion = Companion()
        let connection = BudConnection(base: URL(string: "http://127.0.0.1:8420")!, token: "")
        companion.connectForTesting(connection, session: "chat")
        let file = ChatAttachment(id: "file", name: "plan.md")

        companion.openAttachment(file, in: "first")
        companion.openAttachment(file, in: "second")
        #expect(companion.inspector?.target == .attachment(session: "second", file: file.id))
        companion.openRoutines()
        #expect(companion.inspector?.target == .routine(session: "chat", routine: nil, occurrence: nil))
        companion.openRoutines("routine", occurrence: "run")
        companion.openRoutines()
        #expect(companion.inspector == nil)
        companion.openRoutines()
        companion.openBrowserView()
        #expect(companion.inspector?.target == .browser)
        companion.closeInspector()
        #expect(companion.inspector == nil)
    }
}
