import SwiftUI

struct QuestionAnswer: Equatable, Sendable {
    let header: String
    let selected: [String]
    let custom: String?

    var json: [String: Any] {
        ["header": header, "selected": selected, "custom": custom as Any? ?? NSNull()]
    }

    static func summary(_ answers: [QuestionAnswer]) -> String {
        answers.map { $0.custom ?? $0.selected.joined(separator: ", ") }.joined(separator: "; ")
    }
}

struct QuestionAnswers: Equatable {
    let questions: [Question]
    private(set) var index = 0
    private var given: [Int: QuestionAnswer] = [:]

    init(questions: [Question]) {
        self.questions = questions
    }

    var isFirst: Bool { index == 0 }
    var isLast: Bool { index == questions.count - 1 }
    var current: Question { questions[index] }
    var picked: [String] { given[index]?.selected ?? [] }
    var typed: String? { given[index]?.custom }

    mutating func choose(_ label: String) {
        if !current.multiSelect {
            given[index] = QuestionAnswer(header: current.header, selected: [label], custom: nil)
            return
        }
        var labels = picked
        if let at = labels.firstIndex(of: label) {
            labels.remove(at: at)
        } else {
            labels.append(label)
        }
        given[index] = labels.isEmpty ? nil : QuestionAnswer(
            header: current.header, selected: labels, custom: nil)
    }

    mutating func type(_ text: String, at position: Int) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, questions.indices.contains(position) else { return }
        given[position] = QuestionAnswer(header: questions[position].header, selected: [], custom: trimmed)
    }

    mutating func show(_ position: Int) {
        guard questions.indices.contains(position) else { return }
        index = position
    }

    var completedAnswers: [QuestionAnswer]? {
        var all: [QuestionAnswer] = []
        for position in questions.indices {
            guard let answer = given[position] else { return nil }
            all.append(answer)
        }
        return all
    }
}

@MainActor
final class QuestionFlow: ObservableObject {
    @Published var answers: QuestionAnswers
    @Published var forward = true
    private(set) var typingAt = 0
    private let submit: ([QuestionAnswer]) -> Void

    init(questions: [Question], submit: @escaping ([QuestionAnswer]) -> Void) {
        answers = QuestionAnswers(questions: questions)
        self.submit = submit
    }

    func advance() {
        if answers.isLast {
            sendIfComplete()
        } else {
            move(to: answers.index + 1)
        }
    }

    func back() { move(to: answers.index - 1) }
    func startTyping() { typingAt = answers.index }

    func answerTyped(_ text: String) {
        answers.type(text, at: typingAt)
        if typingAt < answers.questions.count - 1 {
            move(to: typingAt + 1)
        } else {
            sendIfComplete()
        }
    }

    private func move(to position: Int) {
        guard answers.questions.indices.contains(position) else { return }
        forward = position > answers.index
        answers.show(position)
    }

    private func sendIfComplete() {
        if let all = answers.completedAnswers { submit(all) }
    }
}

struct QuestionCards: View {
    let companion: Companion
    let approval: Approval
    @ObservedObject var flow: QuestionFlow
    let listLimit: CGFloat
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var answers: QuestionAnswers { flow.answers }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if answers.questions.count > 1 { navigation }
            ZStack(alignment: .topLeading) {
                QuestionPage(
                    question: answers.current, picked: answers.picked, typed: answers.typed,
                    isLast: answers.isLast, listLimit: listLimit, onPick: pick,
                    onNext: { flow.advance() },
                    onSomethingElse: {
                        flow.startTyping()
                        companion.write(.answer(approval.id))
                    }
                )
                .id(answers.index)
                .transition(transition)
            }
            .clipped()
            .animation(.easeOut(duration: 0.25), value: answers.index)
        }
    }

    func pick(_ label: String) {
        flow.answers.choose(label)
        if !answers.current.multiSelect { flow.advance() }
    }

    private var transition: AnyTransition {
        if reduceMotion { return .opacity }
        return .asymmetric(
            insertion: .move(edge: flow.forward ? .trailing : .leading),
            removal: .move(edge: flow.forward ? .leading : .trailing))
    }

    private var navigation: some View {
        HStack(spacing: 6) {
            Button { flow.back() } label: { Image(systemName: "chevron.left") }
                .buttonStyle(IconButton(size: 22))
                .disabled(answers.isFirst)
                .accessibilityLabel("Previous question")
                .help("Previous question")
            Text("\(answers.index + 1) of \(answers.questions.count)")
                .font(.caption)
                .foregroundStyle(Paper.inkSecondary)
                .accessibilityLabel("Question \(answers.index + 1) of \(answers.questions.count)")
            Spacer(minLength: 0)
        }
    }
}

private struct QuestionPage: View {
    let question: Question
    let picked: [String]
    let typed: String?
    let isLast: Bool
    let listLimit: CGFloat
    let onPick: (String) -> Void
    let onNext: () -> Void
    let onSomethingElse: () -> Void
    @StateObject private var layout = ApprovalLayout()

    private var canSend: Bool { !picked.isEmpty || typed != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    Text(question.text)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 4)
                    ForEach(Array(question.options.enumerated()), id: \.offset) { index, option in
                        Button { onPick(option.label) } label: { optionRow(option) }
                            .buttonStyle(ChoiceRow())
                            .accessibilityLabel(option.description.map { "\(option.label), \($0)" } ?? option.label)
                            .accessibilityAddTraits(picked.contains(option.label) ? .isSelected : [])
                            .modifier(Stagger(index: index))
                    }
                    Button(action: onSomethingElse) { answerRow }
                        .buttonStyle(ChoiceRow())
                        .accessibilityLabel(question.options.isEmpty ? "Answer in your own words" : "Something else")
                        .modifier(Stagger(index: question.options.count))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { layout.height = $0 }
                .padding(.trailing, 6)
            }
            .frame(height: min(layout.height, listLimit))
            .scrollBounceBehavior(.basedOnSize)
            if question.multiSelect || canSend {
                HStack(spacing: 0) {
                    Spacer(minLength: 0)
                    Button(isLast ? "Send" : "Next", action: onNext)
                        .buttonStyle(CardButton(kind: .primary))
                        .fixedSize()
                        .disabled(!canSend)
                        .opacity(canSend ? 1 : 0.45)
                }
            }
        }
    }

    private func optionRow(_ option: QuestionOption) -> some View {
        let selected = picked.contains(option.label)
        let symbol = question.multiSelect
            ? (selected ? "checkmark.circle.fill" : "circle")
            : (selected ? "checkmark" : "arrow.right")
        return HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(option.label).fixedSize(horizontal: false, vertical: true)
                if let description = option.description {
                    Text(description).font(.caption).foregroundStyle(Paper.inkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            RowSymbol(name: symbol)
        }
        .font(.callout)
    }

    private var answerRow: some View {
        HStack(spacing: 8) {
            Text(typed ?? (question.options.isEmpty ? "Answer…" : "Something else…"))
                .foregroundStyle(typed == nil ? Paper.inkSecondary : Paper.inkPrimary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            RowSymbol(name: typed == nil ? "pencil" : "checkmark")
        }
        .font(.callout)
    }
}
