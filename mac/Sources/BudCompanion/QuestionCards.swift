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
    var canAdvance: Bool { completedAnswer(at: index) != nil }

    func typed(at position: Int) -> String? { given[position]?.custom }

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
        given[position] = QuestionAnswer(header: questions[position].header, selected: [], custom: text)
    }

    mutating func show(_ position: Int) {
        guard questions.indices.contains(position) else { return }
        index = position
    }

    var completedAnswers: [QuestionAnswer]? {
        var all: [QuestionAnswer] = []
        for position in questions.indices {
            guard let answer = completedAnswer(at: position) else { return nil }
            all.append(answer)
        }
        return all
    }

    private func completedAnswer(at position: Int) -> QuestionAnswer? {
        guard let answer = given[position] else { return nil }
        guard let custom = answer.custom else { return answer }
        let trimmed = custom.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        return QuestionAnswer(header: answer.header, selected: [], custom: trimmed)
    }
}

@MainActor
final class QuestionFlow: ObservableObject {
    @Published var answers: QuestionAnswers
    private let submit: ([QuestionAnswer]) -> Void

    init(questions: [Question], submit: @escaping ([QuestionAnswer]) -> Void) {
        answers = QuestionAnswers(questions: questions)
        self.submit = submit
    }

    func advance() {
        guard answers.canAdvance else { return }
        if answers.isLast {
            if let all = answers.completedAnswers { submit(all) }
        } else {
            answers.show(answers.index + 1)
        }
    }

    func back() { answers.show(answers.index - 1) }
}

struct QuestionCards: View {
    let companion: Companion
    let approval: Approval
    @ObservedObject var flow: QuestionFlow
    let listLimit: CGFloat
    @Environment(\.replyType) private var replyType

    private var answers: QuestionAnswers { flow.answers }
    private var inWindow: Bool { !replyType.compact }

    var body: some View {
        if inWindow {
            windowBody
        } else {
            compactBody
        }
    }

    private var compactBody: some View {
        VStack(alignment: .leading, spacing: 10) {
            if answers.questions.count > 1 { navigation }
            QuestionPage(
                question: answers.current, picked: answers.picked, canSend: answers.canAdvance,
                showsFooter: answers.current.multiSelect || answers.current.options.isEmpty
                    || answers.typed(at: answers.index) != nil || answers.canAdvance,
                customAnswer: inlineAnswer, isLast: answers.isLast, listLimit: listLimit,
                onPick: pick, onNext: flow.advance
            )
            .id(answers.index)
        }
    }

    private var windowBody: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .center, spacing: 8) {
                    Text(answers.questions.count > 1
                        ? "Question \(answers.index + 1) of \(answers.questions.count)"
                        : "Question")
                        .font(Paper.Typography.captionMedium)
                        .foregroundStyle(Paper.inkMuted)
                    if let job = approval.job {
                        Text("· \(job)")
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkMuted)
                            .lineLimit(1)
                    }
                    Spacer(minLength: 4)
                    if answers.questions.count > 1 {
                        Button { flow.back() } label: { Image(systemName: "chevron.left") }
                            .buttonStyle(IconButton(size: 22))
                            .disabled(answers.isFirst)
                            .opacity(answers.isFirst ? 0.35 : 1)
                            .accessibilityLabel("Previous question")
                            .help("Previous question")
                    }
                }

                WindowQuestionPage(
                    question: answers.current, picked: answers.picked, customAnswer: inlineAnswer,
                    listLimit: max(listLimit, 240), onSelect: { flow.answers.choose($0) }
                )
                .id(answers.index)

                if let problem = companion.approvalFailures[approval.id] ?? companion.decisionProblem {
                    Text(problem)
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.errorInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 14)

            Rectangle().fill(Paper.hairline).frame(height: 0.5)

            HStack(spacing: 8) {
                if answers.questions.count > 1 && !answers.isFirst {
                    Button("Back") { flow.back() }
                        .buttonStyle(CardButton(kind: .quiet))
                }
                Spacer(minLength: 8)
                Button(answers.isLast ? "Submit" : "Next", action: flow.advance)
                    .buttonStyle(CardButton(kind: .primary))
                    .disabled(!answers.canAdvance)
                    .opacity(answers.canAdvance ? 1 : 0.45)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        }
    }

    func pick(_ label: String) {
        flow.answers.choose(label)
        if !answers.current.multiSelect { flow.advance() }
    }

    private var inlineAnswer: some View {
        let position = answers.index
        return InlineCustomAnswer(
            text: Binding(
                get: { flow.answers.typed(at: position) },
                set: { flow.answers.type($0 ?? "", at: position) }
            ),
            hasOptions: !answers.current.options.isEmpty,
            onSubmit: flow.advance,
            onFocus: { companion.focusQuestion(approval.id, position: position, focused: $0) }
        )
    }

    private var navigation: some View {
        HStack(spacing: 6) {
            Button { flow.back() } label: { Image(systemName: "chevron.left") }
                .buttonStyle(IconButton(size: 22))
                .disabled(answers.isFirst)
                .accessibilityLabel("Previous question")
                .help("Previous question")
            Text("\(answers.index + 1) of \(answers.questions.count)")
                .font(Paper.Typography.ledgerMeta)
                .foregroundStyle(Paper.inkSecondary)
                .accessibilityLabel("Question \(answers.index + 1) of \(answers.questions.count)")
            Spacer(minLength: 0)
        }
    }
}

/// Structured question page for the full Conversation Window: bold question title, helper line,
/// and rounded selectable option rows with left radio/check indicator and right numeric badge.
private struct WindowQuestionPage<CustomAnswer: View>: View {
    let question: Question
    let picked: [String]
    let customAnswer: CustomAnswer
    let listLimit: CGFloat
    let onSelect: (String) -> Void
    @StateObject private var layout = ApprovalLayout()

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(question.text)
                    .font(Fonts.sans(size: 14.5, weight: Fonts.OpticalWeight.semibold))
                    .foregroundStyle(Paper.inkPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                if !question.options.isEmpty {
                    Text(question.multiSelect ? "Select one or more answers" : "Select one answer")
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkMuted)
                }
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(question.options.enumerated()), id: \.offset) { index, option in
                        WindowOptionRow(
                            option: option,
                            index: index + 1,
                            selected: picked.contains(option.label),
                            multiSelect: question.multiSelect,
                            action: { onSelect(option.label) }
                        )
                    }
                    customAnswer
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { layout.height = $0 }
            }
            .frame(height: min(layout.height, listLimit))
            .scrollBounceBehavior(.basedOnSize)
        }
    }
}

private struct WindowOptionRow: View {
    let option: QuestionOption
    let index: Int
    let selected: Bool
    let multiSelect: Bool
    let action: () -> Void

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 8, style: .continuous)
        Button(action: action) {
            HStack(alignment: .center, spacing: 10) {
                indicator
                VStack(alignment: .leading, spacing: 2) {
                    Text(option.label)
                        .font(Fonts.sans(
                            size: 13,
                            weight: selected ? Fonts.OpticalWeight.medium : Fonts.OpticalWeight.regular))
                        .foregroundStyle(Paper.inkPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let description = option.description {
                        Text(description)
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 6)
                Text("\(index)")
                    .font(Paper.Typography.ledgerMeta)
                    .foregroundStyle(selected ? Paper.inkPrimary : Paper.inkMuted)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(
                        selected ? Paper.fillHover : Paper.recessed,
                        in: RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous)
                            .strokeBorder(Paper.borderSubtle, lineWidth: 0.5))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                selected ? Paper.fillHover : Paper.fillSubtle,
                in: shape
            )
            .overlay(
                shape.strokeBorder(
                    selected ? Paper.borderStrong : Paper.borderSubtle,
                    lineWidth: selected ? 1 : 0.5)
            )
            .contentShape(shape)
        }
        .buttonStyle(PaperHit(shape: shape))
        .accessibilityLabel(option.description.map { "\(option.label), \($0)" } ?? option.label)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    @ViewBuilder
    private var indicator: some View {
        if multiSelect {
            let box = RoundedRectangle(cornerRadius: 4, style: .continuous)
            ZStack {
                if selected {
                    box.fill(Paper.inkPrimary)
                    Image(systemName: "checkmark")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Paper.canvas)
                } else {
                    box.strokeBorder(Paper.borderStrong, lineWidth: 1)
                }
            }
            .frame(width: 15, height: 15)
        } else {
            ZStack {
                Circle()
                    .strokeBorder(
                        selected ? Paper.inkPrimary : Paper.borderStrong,
                        lineWidth: selected ? 1.5 : 1)
                if selected {
                    Circle()
                        .fill(Paper.inkPrimary)
                        .frame(width: 7, height: 7)
                }
            }
            .frame(width: 15, height: 15)
        }
    }
}

private struct InlineCustomAnswer: View {
    @Binding var text: String?
    let hasOptions: Bool
    let onSubmit: () -> Void
    let onFocus: (Bool) -> Void
    @Environment(\.replyType) private var replyType
    @FocusState private var focused: Bool

    var body: some View {
        content
            .onChange(of: focused) { _, active in onFocus(active) }
            .onDisappear { onFocus(false) }
    }

    @ViewBuilder private var content: some View {
        let shape = RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
        if text != nil || !hasOptions {
            row {
                ChatInput(
                    title: "Your answer",
                    text: Binding(get: { text ?? "" }, set: { text = $0 }),
                    focus: $focused, submit: onSubmit
                )
                .lineLimit(1...4)
            }
            .background(Paper.fillSubtle, in: shape)
            .overlay {
                shape.strokeBorder(
                    focused ? Paper.borderStrong : Paper.borderSubtle,
                    lineWidth: focused ? 1 : 0.5)
            }
        } else {
            Button {
                if replyType.compact { PetPanel.current?.makeKey() }
                text = ""
                focused = true
            } label: {
                row {
                    Text("Something else…").foregroundStyle(Paper.inkSecondary)
                    Spacer(minLength: 0)
                }
                .overlay { shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5) }
            }
            .buttonStyle(PaperHit(shape: shape, fill: Paper.fillSubtle))
            .accessibilityLabel("Something else")
        }
    }

    private func row(@ViewBuilder content: () -> some View) -> some View {
        HStack(spacing: 10) {
            Image(systemName: "pencil")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Paper.inkSecondary)
                .frame(width: 15)
                .accessibilityHidden(true)
            content()
        }
        .font(Paper.Typography.callout)
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct QuestionPage<CustomAnswer: View>: View {
    let question: Question
    let picked: [String]
    let canSend: Bool
    let showsFooter: Bool
    let customAnswer: CustomAnswer
    let isLast: Bool
    let listLimit: CGFloat
    let onPick: (String) -> Void
    let onNext: () -> Void
    @StateObject private var layout = ApprovalLayout()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    Text(question.text)
                        .font(Paper.Typography.callout)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.bottom, 4)
                    ForEach(Array(question.options.enumerated()), id: \.offset) { _, option in
                        Button { onPick(option.label) } label: { optionRow(option) }
                            .buttonStyle(ChoiceRow())
                            .accessibilityLabel(option.description.map { "\(option.label), \($0)" } ?? option.label)
                            .accessibilityAddTraits(picked.contains(option.label) ? .isSelected : [])
                    }
                    customAnswer
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { layout.height = $0 }
                .padding(.trailing, 6)
            }
            .frame(height: min(layout.height, listLimit))
            .scrollBounceBehavior(.basedOnSize)
            if showsFooter {
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
                    Text(description)
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            RowSymbol(name: symbol)
        }
        .font(Paper.Typography.callout)
    }
}
