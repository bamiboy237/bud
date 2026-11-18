import Foundation
import Testing
@testable import BudCompanion

struct QuestionTests {
    private static var listArgs: [String: Any] { ["questions": [
        ["header": "Which Sam", "question": "Which Sam do you mean?",
         "options": [["label": "Sam Lee", "description": "your manager"], ["label": "Sam Park"]]],
        ["header": "Day", "question": "Which day works?",
         "options": [["label": "Wed"], ["label": "Thu"]], "multi_select": true],
    ]] }

    @Test
    func parsesListAndLegacyQuestions() throws {
        let list = Question.parse(Self.listArgs)
        #expect(list.isList)
        #expect(list.questions.map(\.header) == ["Which Sam", "Day"])
        #expect(list.questions[0].options[0].description == "your manager")
        #expect(!list.questions[0].multiSelect)
        #expect(list.questions[1].multiSelect)
        let legacy = Question.parse(["question": "Which one?", "choices": ["A", "B"]])
        #expect(!legacy.isList)
        let question = try #require(legacy.questions.first)
        #expect(question.header == "Question")
        #expect(question.options.map(\.label) == ["A", "B"])
        #expect(Question.parse(["questions": [
            ["header": "Name", "question": "What name?", "options": [] as [[String: Any]]],
        ]]).questions.first?.options.isEmpty == true)
    }

    @Test
    func onlyCompleteAnswersAreEncodedInQuestionOrder() throws {
        var state = QuestionAnswers(questions: Question.parse(Self.listArgs).questions)
        state.type("  Sam from finance  ", at: 0)
        #expect(state.completedAnswers == nil)
        state.show(1)
        state.choose("Wed")
        state.choose("Thu")
        state.choose("Wed")
        let answers = try #require(state.completedAnswers)
        #expect(answers == [
            QuestionAnswer(header: "Which Sam", selected: [], custom: "Sam from finance"),
            QuestionAnswer(header: "Day", selected: ["Thu"], custom: nil),
        ])
        #expect(answers[0].json["custom"] as? String == "Sam from finance")
        #expect(answers[1].json["custom"] is NSNull)
        state.show(0)
        state.choose("Sam Lee")
        #expect(state.completedAnswers?.first?.selected == ["Sam Lee"])
        #expect(state.completedAnswers?.first?.custom == nil)
        state.show(1)
        state.choose("Thu")
        #expect(state.completedAnswers == nil)
        state.type("   ", at: 1)
        #expect(state.completedAnswers == nil)
    }

    @Test @MainActor
    func legacyNumericChoiceSubmitsSelectedLabel() throws {
        let approval = try #require(Approval([
            "approval_id": "question", "tool": "ask_user",
            "args": ["question": "Which one?", "choices": ["2", "3"]],
        ], jobs: [:]))
        var sent: [[QuestionAnswer]] = []
        let flow = QuestionFlow(questions: approval.questions) { sent.append($0) }
        let cards = QuestionCards(companion: Companion(), approval: approval, flow: flow, listLimit: 200)

        cards.pick("2")

        #expect(sent == [[QuestionAnswer(header: "Question", selected: ["2"], custom: nil)]])
    }

    @Test @MainActor
    func backPreservesAnswersAndFinalAdvanceSubmits() {
        var sent: [[QuestionAnswer]] = []
        let flow = QuestionFlow(questions: Question.parse(Self.listArgs).questions) { sent.append($0) }
        flow.answers.choose("Sam Lee")
        flow.advance()
        flow.answers.choose("Wed")
        flow.back()
        #expect(flow.answers.picked == ["Sam Lee"])
        flow.advance()
        #expect(flow.answers.picked == ["Wed"])
        #expect(sent.isEmpty)
        flow.advance()
        #expect(sent.count == 1)
        #expect(sent.first?.map(\.selected) == [["Sam Lee"], ["Wed"]])
    }

    @Test @MainActor
    func typedAnswerAdvancesThenSends() {
        var sent: [[QuestionAnswer]] = []
        let flow = QuestionFlow(questions: Question.parse(Self.listArgs).questions) { sent.append($0) }
        flow.startTyping()
        flow.answerTyped("Sam Chen")
        #expect(flow.answers.index == 1)
        #expect(sent.isEmpty)
        flow.answers.choose("Thu")
        flow.advance()
        #expect(sent.count == 1)
        #expect(sent.first?.first?.custom == "Sam Chen")
    }

    @Test @MainActor
    func customAnswerRestoresTheChatDraft() throws {
        let approval = try #require(Approval([
            "approval_id": "question", "tool": "ask_user", "args": Self.listArgs,
        ], jobs: [:]))
        let companion = Companion()
        companion.draft = "My next request"
        companion.write(.answer(approval.id))
        let flow = companion.questionFlow(for: approval)
        flow.startTyping()
        companion.draft = "Sam Chen"
        companion.answerQuestionText(companion.draft, for: approval)
        #expect(companion.inputPurpose == .ask)
        #expect(companion.draft == "My next request")
        #expect(companion.inputOpen)
        #expect(flow.answers.index == 1)
        flow.back()
        #expect(flow.answers.typed == "Sam Chen")
    }
}
