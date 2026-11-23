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
    var onDismiss: (() -> Void)? = nil
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.replyType) private var replyType

    private var answers: QuestionAnswers { flow.answers }
    private var inWindow: Bool { !replyType.compact || companion.windowOpen }

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

    private var windowBody: some View {
        let canSend = !answers.picked.isEmpty || answers.typed != nil
        return VStack(alignment: .leading, spacing: 0) {
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

                ZStack(alignment: .topLeading) {
                    WindowQuestionPage(
                        question: answers.current,
                        picked: answers.picked,
                        typed: answers.typed,
                        listLimit: max(listLimit, 240),
                        onSelect: { label in flow.answers.choose(label) },
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
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 14)

            Rectangle().fill(Paper.hairline).frame(height: 0.5)

            HStack(spacing: 8) {
                if let onDismiss {
                    Button("Dismiss", action: onDismiss)
                        .buttonStyle(CardButton(kind: .quiet))
                }
                Spacer(minLength: 8)
                Button(answers.isLast ? "Submit" : "Next") {
                    withTransaction(Transaction(animation: nil)) {
                        flow.advance()
                    }
                }
                .buttonStyle(CardButton(kind: .primary))
                .disabled(!canSend)
                .opacity(canSend ? 1 : 0.45)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
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
                .font(Paper.Typography.ledgerMeta)
                .foregroundStyle(Paper.inkSecondary)
                .accessibilityLabel("Question \(answers.index + 1) of \(answers.questions.count)")
            Spacer(minLength: 0)
        }
    }
}

/// Structured question page for the full Conversation Window: bold question title, helper line,
/// and rounded selectable option rows with left radio/check indicator and right numeric badge.
private struct WindowQuestionPage: View {
    let question: Question
    let picked: [String]
    let typed: String?
    let listLimit: CGFloat
    let onSelect: (String) -> Void
    let onSomethingElse: () -> Void
    @StateObject private var layout = ApprovalLayout()

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(question.text)
                    .font(Fonts.sans(size: 14.5, weight: Fonts.OpticalWeight.semibold))
                    .foregroundStyle(Paper.inkPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(question.multiSelect ? "Select one or more answers" : "Select one answer")
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.inkMuted)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(question.options.enumerated()), id: \.offset) { index, option in
                        WindowOptionRow(
                            option: option,
                            index: index + 1,
                            selected: picked.contains(option.label),
                            multiSelect: question.multiSelect,
                            action: {
                                withTransaction(Transaction(animation: nil)) {
                                    onSelect(option.label)
                                }
                            }
                        )
                        .modifier(Stagger(index: index))
                    }
                    WindowCustomAnswerRow(
                        typed: typed,
                        hasOptions: !question.options.isEmpty,
                        action: onSomethingElse
                    )
                    .modifier(Stagger(index: question.options.count))
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
    @StateObject private var hovered = Flag()

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
                    .foregroundStyle(selected ? Paper.citationInk : Paper.inkMuted)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(
                        selected ? Paper.citationFill : Paper.recessed,
                        in: RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous)
                            .strokeBorder(
                                selected ? Paper.citationBorder : Paper.borderSubtle,
                                lineWidth: 0.5))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                selected ? Paper.citationFill : hovered.on ? Paper.fillHover : Paper.fillSubtle,
                in: shape
            )
            .overlay(
                shape.strokeBorder(
                    selected ? Paper.accent : Paper.borderSubtle,
                    lineWidth: selected ? 1 : 0.5)
            )
            .contentShape(shape)
        }
        .buttonStyle(PressScaleOptionStyle())
        .pointerStyle(.link)
        .onHover { hovered.on = $0 }
        .accessibilityLabel(option.description.map { "\(option.label), \($0)" } ?? option.label)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    @ViewBuilder
    private var indicator: some View {
        if multiSelect {
            let box = RoundedRectangle(cornerRadius: 4, style: .continuous)
            ZStack {
                if selected {
                    box.fill(Paper.accent)
                    Image(systemName: "checkmark")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(Paper.canvas)
                } else {
                    box.strokeBorder(hovered.on ? Paper.inkSecondary : Paper.borderStrong, lineWidth: 1)
                }
            }
            .frame(width: 15, height: 15)
        } else {
            ZStack {
                Circle()
                    .strokeBorder(
                        selected ? Paper.accent : hovered.on ? Paper.inkSecondary : Paper.borderStrong,
                        lineWidth: selected ? 1.5 : 1)
                if selected {
                    Circle()
                        .fill(Paper.accent)
                        .frame(width: 7, height: 7)
                }
            }
            .frame(width: 15, height: 15)
        }
    }
}

private struct WindowCustomAnswerRow: View {
    let typed: String?
    let hasOptions: Bool
    let action: () -> Void
    @StateObject private var hovered = Flag()

    var body: some View {
        let hasCustom = typed != nil
        let shape = RoundedRectangle(cornerRadius: 8, style: .continuous)
        Button(action: action) {
            HStack(alignment: .center, spacing: 10) {
                Image(systemName: hasCustom ? "checkmark.circle.fill" : "pencil")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(hasCustom ? Paper.accent : Paper.inkMuted)
                    .frame(width: 15, height: 15)
                Text(typed ?? (hasOptions ? "Something else…" : "Answer in your own words…"))
                    .font(Paper.Typography.callout)
                    .foregroundStyle(hasCustom ? Paper.inkPrimary : Paper.inkSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 6)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                hasCustom ? Paper.citationFill : hovered.on ? Paper.fillHover : Paper.fillSubtle,
                in: shape
            )
            .overlay(
                shape.strokeBorder(
                    hasCustom ? Paper.accent : Paper.borderSubtle,
                    lineWidth: hasCustom ? 1 : 0.5)
            )
            .contentShape(shape)
        }
        .buttonStyle(PressScaleOptionStyle())
        .pointerStyle(.link)
        .onHover { hovered.on = $0 }
        .accessibilityLabel(hasOptions ? "Something else" : "Answer in your own words")
    }
}

private struct PressScaleOptionStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(!reduceMotion && configuration.isPressed ? 0.97 : 1.0)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
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
                        .font(Paper.Typography.callout)
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

    private var answerRow: some View {
        HStack(spacing: 8) {
            Text(typed ?? (question.options.isEmpty ? "Answer…" : "Something else…"))
                .foregroundStyle(typed == nil ? Paper.inkSecondary : Paper.inkPrimary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            RowSymbol(name: typed == nil ? "pencil" : "checkmark")
        }
        .font(Paper.Typography.callout)
    }
}
