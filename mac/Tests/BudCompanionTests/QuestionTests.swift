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
        #expect(list.map(\.header) == ["Which Sam", "Day"])
        #expect(list[0].options[0].description == "your manager")
        #expect(!list[0].multiSelect)
        #expect(list[1].multiSelect)
        let legacy = Question.parse(["question": "Which one?", "choices": ["A", "B"]])
        let question = try #require(legacy.first)
        #expect(question.header == "Question")
        #expect(question.options.map(\.label) == ["A", "B"])
        #expect(Question.parse(["questions": [
            ["header": "Name", "question": "What name?", "options": [] as [[String: Any]]],
        ]]).first?.options.isEmpty == true)
    }

    @Test
    func onlyCompleteAnswersAreEncodedInQuestionOrder() throws {
        var state = QuestionAnswers(questions: Question.parse(Self.listArgs))
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
        let flow = QuestionFlow(questions: Question.parse(Self.listArgs)) { sent.append($0) }
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
    func inlineAnswerRequiresTextBeforeAdvancing() {
        var sent: [[QuestionAnswer]] = []
        let flow = QuestionFlow(questions: Question.parse(Self.listArgs)) { sent.append($0) }
        flow.answers.type("   ", at: 0)
        flow.advance()
        #expect(flow.answers.index == 0)
        #expect(!flow.answers.canAdvance)
        #expect(sent.isEmpty)

        flow.answers.type(" Sam Chen ", at: 0)
        #expect(flow.answers.typed(at: 0) == " Sam Chen ")
        #expect(sent.isEmpty)
        flow.advance()
        #expect(flow.answers.index == 1)
        #expect(sent.isEmpty)
        flow.answers.choose("Thu")
        flow.advance()
        #expect(sent.count == 1)
        #expect(sent.first?.first?.custom == "Sam Chen")
        flow.back()
        #expect(flow.answers.typed(at: 0) == " Sam Chen ")
    }

    @Test(.timeLimit(.minutes(1))) @MainActor
    func rejectedInlineAnswerKeepsTheCardAndComposerDrafts() async throws {
        let companion = Companion()
        let (requests, continuation) = AsyncStream.makeStream(of: URLRequest.self)
        companion.connectForTesting(BudConnection(
            base: URL(string: "http://127.0.0.1:8420")!, token: ""
        ) { request in
            continuation.yield(request)
            if request.httpMethod == "GET" { continuation.finish() }
            return (
                Data(#"{"detail":"Try again"}"#.utf8),
                HTTPURLResponse(url: request.url!, statusCode: 503, httpVersion: nil, headerFields: nil)!
            )
        })
        companion.apply(snapshot: [
            "last_seq": 1, "pending_approvals": [[
                "approval_id": "question", "tool": "ask_user", "args": Self.listArgs,
            ]],
        ])
        let approval = try #require(companion.approvals.first)
        companion.draft = "My next request"
        let file = DraftAttachment(reference: ChatAttachment(id: "file", name: "notes.md"), data: Data())
        companion.draftAttachments = [file]
        companion.inputOpen = true
        let flow = companion.questionFlow(for: approval)
        flow.answers.type("Sam Chen", at: 0)
        flow.advance()
        flow.answers.type("Friday", at: 1)
        flow.advance()

        var posts: [URLRequest] = []
        for await request in requests where request.httpMethod == "POST" { posts.append(request) }
        let post = try #require(posts.first)
        let body = try #require(post.httpBody)
        let payload = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        let answers = try #require(payload["answers"] as? [[String: Any]])
        #expect(posts.count == 1)
        #expect(post.url?.path == "/sessions/s1/approvals/question/answer")
        #expect(answers.compactMap { $0["custom"] as? String } == ["Sam Chen", "Friday"])
        #expect(companion.decisionProblem?.contains("Try again") == true)
        #expect(companion.inputPurpose == .ask)
        #expect(companion.draft == "My next request")
        #expect(companion.draftAttachments == [file])
        #expect(companion.inputOpen)
        #expect(flow.answers.index == 1)
        #expect(flow.answers.typed(at: 1) == "Friday")
    }
}
