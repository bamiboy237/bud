import SwiftUI

final class ApprovalLayout: ObservableObject {
    @Published var height: CGFloat = 1
    @Published var message = 0
}

enum DecisionDirection: Equatable {
    case approve
    case deny
}

final class DeckState: ObservableObject {
    @Published var currentIndex: Int = 0
    @Published var flyOpacity: Double = 1.0
    @Published var activeDecision: DecisionDirection? = nil
    @Published var isAnimating: Bool = false

    func finishDecision() {
        flyOpacity = 1
        activeDecision = nil
        isAnimating = false
    }
}

/// A stacked deck of pending approvals. Cards sit directly on top of each other with subtle
/// depth offsets, keeping the vertical footprint constant regardless of how many actions wait.
/// Deciding an action lets the card drift a little toward the choice and fade, without tilt,
/// color, or bounce, while the card behind settles forward. The receipt line says what
/// happened.
struct ApprovalDeck: View {
    @Bindable var companion: Companion
    @StateObject private var deck = DeckState()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Lands without overshoot: the deck reports state, so it never shows one it isn't in.
    static let settle = Animation.spring(duration: 0.4, bounce: 0)

    private var totalCount: Int { companion.approvals.count }
    private var safeIndex: Int {
        totalCount == 0 ? 0 : min(max(deck.currentIndex, 0), totalCount - 1)
    }
    private var currentApproval: Approval? {
        guard safeIndex < totalCount else { return nil }
        return companion.approvals[safeIndex]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // A count and arrows only mean something with more than one card.
            if totalCount > 1 { headerBar }
            cardStack
        }
        .onAppear { clampIndex() }
        .onChange(of: companion.approvals.map(\.id)) { _, _ in clampIndex() }
        // A new front card always starts at home, whatever happened to the last one.
        .onChange(of: currentApproval?.id) { _, _ in
            if !deck.isAnimating { deck.finishDecision() }
        }
    }

    private func clampIndex() {
        if deck.currentIndex >= totalCount {
            deck.currentIndex = max(0, totalCount - 1)
        }
    }

    private func cyclePrevious() {
        guard !deck.isAnimating, deck.currentIndex > 0 else { return }
        withAnimation(Self.settle) {
            deck.currentIndex -= 1
        }
    }

    private func cycleNext() {
        guard !deck.isAnimating, deck.currentIndex < totalCount - 1 else { return }
        withAnimation(Self.settle) {
            deck.currentIndex += 1
        }
    }

    private var headerBar: some View {
        HStack(alignment: .center, spacing: 8) {
            Label {
                Text("Approval \(min(safeIndex + 1, totalCount)) of \(totalCount)")
                    .font(Paper.Typography.ledgerMeta)
                    .foregroundStyle(Paper.inkSecondary)
            } icon: {
                Image(systemName: "doc.on.doc.fill")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Paper.inkSecondary)
            }
            Spacer(minLength: 4)

            if totalCount > 1 {
                HStack(spacing: 4) {
                    DeckArrowButton(
                        symbol: "chevron.left",
                        disabled: deck.isAnimating || safeIndex == 0,
                        helpText: "View previous card in stack",
                        action: cyclePrevious
                    )
                    DeckArrowButton(
                        symbol: "chevron.right",
                        disabled: deck.isAnimating || safeIndex >= totalCount - 1,
                        helpText: "View next card in stack",
                        action: cycleNext
                    )
                }
            }

            if let batch = currentBatch {
                batchButton(batch)
            }
        }
        .padding(.horizontal, 2)
    }

    private var currentBatch: PendingBatch? {
        guard let current = currentApproval, let key = current.requestID else { return nil }
        let items = current.batch(in: companion.approvals)
        guard items.count > 1 else { return nil }
        return PendingBatch(
            id: key,
            approval: current,
            items: items,
            failed: current.retryBatch(in: companion.approvals, failures: companion.approvalFailures)
        )
    }

    private func batchButton(_ batch: PendingBatch) -> some View {
        let failed = batch.failed
        return Button {
            if failed.isEmpty {
                companion.approveAll(for: batch.approval)
            } else {
                companion.retryFailed(for: batch.approval)
            }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 11))
                Text(companion.approvingBatch ? "Approving…" :
                        failed.isEmpty ? "All (\(batch.items.count))" : "Retry (\(failed.count))")
                    .font(Paper.Typography.captionMedium)
            }
        }
        .buttonStyle(CardButton(kind: .secondary))
        .disabled(!companion.deciding.isEmpty || companion.approvingBatch || companion.link != .connected)
        .help(failed.isEmpty ? "Approve all actions in this request" : "Retry only failed actions")
    }

    /// Room below the front card for the cards behind it to show.
    private var peekRoom: CGFloat { CGFloat(min(max(totalCount - safeIndex - 1, 0), 2)) * 8 }

    /// The front card sets the deck's height, so a short card leaves no empty space. The cards
    /// behind it take that same size and show only their bottom edges.
    private var cardStack: some View {
        ZStack(alignment: .top) {
            if let activeApproval = currentApproval {
                ApprovalCard(
                    companion: companion, approval: activeApproval,
                    onDecide: { approved in
                        handleDecision(approval: activeApproval, approved: approved)
                    }
                )
                .id(activeApproval.id)
                .opacity(deck.flyOpacity)
            }
        }
        // Exactly as tall as the card: a capped flexible frame grew to its cap and left empty
        // paper under a short card. Cards bound their own long content.
        .frame(maxWidth: .infinity, alignment: .top)
        .fixedSize(horizontal: false, vertical: true)
        .background(alignment: .top) {
            ZStack(alignment: .top) {
                if safeIndex + 2 < totalCount {
                    PeekCard(
                        approval: companion.approvals[safeIndex + 2],
                        offset: 2,
                        isTopAdvancing: deck.activeDecision != nil
                    )
                }
                if safeIndex + 1 < totalCount {
                    PeekCard(
                        approval: companion.approvals[safeIndex + 1],
                        offset: 1,
                        isTopAdvancing: deck.activeDecision != nil
                    )
                }
            }
        }
        .padding(.bottom, peekRoom)
    }

    private func handleDecision(approval: Approval, approved: Bool) {
        guard !deck.isAnimating else { return }
        let flight = Duration.milliseconds(150)
        guard companion.decide(approval.id, approved: approved, after: reduceMotion ? nil : flight)
        else { return }
        guard !reduceMotion else { return }
        deck.isAnimating = true
        deck.activeDecision = approved ? .approve : .deny

        // A quiet fade where the card stands: no slide, no tilt, no stamp. The receipt line
        // that follows says what happened.
        withAnimation(.easeOut(duration: 0.15)) {
            deck.flyOpacity = 0
        }

        Task { @MainActor in
            try? await Task.sleep(for: flight)
            deck.finishDecision()
            clampIndex()
        }
    }
}

private struct DeckArrowButton: View {
    let symbol: String
    let disabled: Bool
    let helpText: String
    let action: () -> Void
    @StateObject private var hovered = Flag()

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(hovered.on && !disabled ? Paper.inkPrimary : Paper.inkSecondary)
                .frame(width: 22, height: 22)
                .background(hovered.on && !disabled ? Paper.fillHover : Paper.fillSubtle, in: shape)
                .overlay(shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5))
        }
        .buttonStyle(.plain)
        .pointerStyle(disabled ? .default : .link)
        .onHover { hovered.on = $0 }
        .disabled(disabled)
        .opacity(disabled ? 0.35 : 1.0)
        .help(helpText)
    }
}

private struct PendingBatch: Identifiable {
    let id: String
    let approval: Approval
    let items: [Approval]
    let failed: [Approval]
}

/// A peek card that sits behind the front card, establishing depth on elevated paper.
struct PeekCard: View {
    let approval: Approval
    let offset: Int // 1 or 2
    let isTopAdvancing: Bool

    private var scale: CGFloat {
        let baseScale: CGFloat = offset == 1 ? 0.95 : 0.90
        let targetScale: CGFloat = offset == 1 ? 1.0 : 0.95
        return isTopAdvancing ? targetScale : baseScale
    }

    private var yOffset: CGFloat {
        let baseY: CGFloat = CGFloat(offset) * 8
        let targetY: CGFloat = CGFloat(offset - 1) * 8
        return isTopAdvancing ? targetY : baseY
    }

    private var opacity: Double {
        let baseOpacity: Double = offset == 1 ? 0.70 : 0.40
        let targetOpacity: Double = offset == 1 ? 1.0 : 0.70
        return isTopAdvancing ? targetOpacity : baseOpacity
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: approval.symbol)
                    .font(.system(size: 11, weight: .medium))
                    .frame(width: 22, height: 22)
                    .background(
                        Paper.fillSubtle,
                        in: RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous))
                    .foregroundStyle(Paper.inkPrimary)
                Text(approval.title)
                    .font(Paper.Typography.label)
                    .lineLimit(1)
                    .foregroundStyle(Paper.inkPrimary)
                Spacer()
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .surface(
            RoundedRectangle(cornerRadius: Paper.radiusMD, style: .continuous),
            lift: 0.75,
            fill: Paper.elevated,
            border: Paper.borderStrong,
            borderWidth: 0.5
        )
        // Shrinks toward its bottom edge, so that edge peeks out below the card in front.
        .scaleEffect(scale, anchor: .bottom)
        .offset(y: yOffset)
        .opacity(opacity)
        .zIndex(Double(5 - offset))
        .animation(ApprovalDeck.settle, value: isTopAdvancing)
        .allowsHitTesting(false)
    }
}

struct EmailBody: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(EmailParagraph.parse(text)) { paragraph in
                Text(verbatim: paragraph.text)
                    .font(Paper.Typography.callout)
                    .lineSpacing(3)
                    .foregroundStyle(paragraph.quoted ? Paper.inkSecondary : Paper.inkPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.leading, paragraph.quoted ? 10 : 0)
                    .overlay(alignment: .leading) {
                        if paragraph.quoted {
                            Rectangle().fill(Paper.borderSubtle).frame(width: 2)
                        }
                    }
            }
        }
        .textSelection(.enabled)
    }
}

struct EmailThreadPreview: View {
    let email: EmailPreview
    @StateObject private var selection = ApprovalLayout()

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let subject = email.subject {
                Text(verbatim: subject)
                    .font(Paper.Typography.label)
                    .foregroundStyle(Paper.inkPrimary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if email.messages.count > 1 {
                Picker("Email in thread", selection: $selection.message) {
                    ForEach(email.messages.indices, id: \.self) { index in
                        Text("\(index + 1). \(email.messages[index].sender ?? "Email")").tag(index)
                    }
                }
                .pickerStyle(.menu)
                .pointerStyle(.link)
            }
            if !email.messages.isEmpty {
                let message = email.messages[min(selection.message, email.messages.count - 1)]
                VStack(alignment: .leading, spacing: 3) {
                    if let sender = message.sender { Text("From: \(sender)") }
                    if !message.to.isEmpty { Text("To: \(message.to.joined(separator: ", "))") }
                    if !message.cc.isEmpty { Text("Cc: \(message.cc.joined(separator: ", "))") }
                    if !message.bcc.isEmpty { Text("Bcc: \(message.bcc.joined(separator: ", "))") }
                    if let date = message.sentAt { Text(date) }
                }
                .font(Paper.Typography.caption)
                .foregroundStyle(Paper.inkSecondary)
                .fixedSize(horizontal: false, vertical: true)
                if let body = message.body {
                    EmailBody(text: body)
                } else {
                    Text("Email body unavailable")
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkSecondary)
                }
            }
            if let reason = email.unavailableReason {
                Text(reason)
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
            }
        }
    }
}
