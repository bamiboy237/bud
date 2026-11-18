import SwiftUI

final class ApprovalLayout: ObservableObject {
    @Published var height: CGFloat = 1
    @Published var message = 0
}

enum DecisionDirection: Equatable {
    case approve
    case deny

    static func swipe(_ translation: CGSize) -> Self? {
        guard abs(translation.width) >= 80,
              abs(translation.width) > abs(translation.height) else { return nil }
        return translation.width > 0 ? .approve : .deny
    }
}

final class DeckState: ObservableObject {
    @Published var currentIndex: Int = 0
    @Published var flyOffset: CGFloat = 0
    @Published var flyRotation: Double = 0
    @Published var flyOpacity: Double = 1.0
    @Published var activeDecision: DecisionDirection? = nil
    @Published var isAnimating: Bool = false

    func finishDecision() {
        flyOffset = 0
        flyRotation = 0
        flyOpacity = 1
        activeDecision = nil
        isAnimating = false
    }
}

/// A stacked deck of pending approvals. Cards sit directly on top of each other with subtle
/// depth offsets, keeping the vertical footprint constant regardless of how many actions wait.
/// Deciding an action tints the card green as it slides right, or red as it slides left,
/// while the card behind smoothly springs forward.
struct ApprovalDeck: View {
    @Bindable var companion: Companion
    @StateObject private var deck = DeckState()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

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
            headerBar
            cardStack
        }
        .onAppear { clampIndex() }
        .onChange(of: companion.approvals.map(\.id)) { _, _ in clampIndex() }
    }

    private func clampIndex() {
        if deck.currentIndex >= totalCount {
            deck.currentIndex = max(0, totalCount - 1)
        }
    }

    private func cyclePrevious() {
        guard !deck.isAnimating, deck.currentIndex > 0 else { return }
        withAnimation(.spring(response: 0.28, dampingFraction: 0.8)) {
            deck.currentIndex -= 1
        }
    }

    private func cycleNext() {
        guard !deck.isAnimating, deck.currentIndex < totalCount - 1 else { return }
        withAnimation(.spring(response: 0.28, dampingFraction: 0.8)) {
            deck.currentIndex += 1
        }
    }

    private var headerBar: some View {
        HStack(alignment: .center, spacing: 8) {
            Label {
                Text("Approval \(min(safeIndex + 1, totalCount)) of \(totalCount)")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.secondary)
            } icon: {
                Image(systemName: "doc.on.doc.fill")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 4)

            if totalCount > 1 {
                HStack(spacing: 4) {
                    Button {
                        cyclePrevious()
                    } label: {
                        Image(systemName: "chevron.left")
                            .font(.system(size: 11, weight: .semibold))
                            .frame(width: 22, height: 22)
                            .background(Paper.ink.opacity(0.06), in: Circle())
                    }
                    .buttonStyle(.plain)
                    .disabled(deck.isAnimating || safeIndex == 0)
                    .opacity(safeIndex == 0 ? 0.35 : 1.0)
                    .help("View previous card in stack")

                    Button {
                        cycleNext()
                    } label: {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 11, weight: .semibold))
                            .frame(width: 22, height: 22)
                            .background(Paper.ink.opacity(0.06), in: Circle())
                    }
                    .buttonStyle(.plain)
                    .disabled(deck.isAnimating || safeIndex >= totalCount - 1)
                    .opacity(safeIndex >= totalCount - 1 ? 0.35 : 1.0)
                    .help("View next card in stack")
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
                    .font(.system(size: 11, weight: .semibold))
            }
        }
        .buttonStyle(CardButton(kind: .secondary))
        .disabled(!companion.deciding.isEmpty || companion.approvingBatch || companion.link != .connected)
        .help(failed.isEmpty ? "Approve all actions in this request" : "Retry only failed actions")
    }

    private var cardStack: some View {
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

            if let activeApproval = currentApproval {
                ApprovalCard(
                    companion: companion, approval: activeApproval,
                    onDecide: { approved in
                        handleDecision(approval: activeApproval, approved: approved)
                    }
                )
                .id(activeApproval.id)
                .overlay(decisionTintOverlay)
                .offset(x: deck.flyOffset, y: 0)
                .rotationEffect(.degrees(deck.flyRotation))
                .opacity(deck.flyOpacity)
                .zIndex(10)
            }
        }
        .frame(minHeight: 245, maxHeight: 275)
    }

    @ViewBuilder
    private var decisionTintOverlay: some View {
        if let decision = deck.activeDecision {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill((decision == .approve ? Color.green : Color.red).opacity(0.12))
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }

    private func handleDecision(approval: Approval, approved: Bool) {
        guard !deck.isAnimating else { return }
        let flight = Duration.milliseconds(220)
        guard companion.decide(approval.id, approved: approved, after: reduceMotion ? nil : flight),
              !reduceMotion else { return }
        deck.isAnimating = true
        deck.activeDecision = approved ? .approve : .deny

        withAnimation(.spring(response: 0.30, dampingFraction: 0.75)) {
            deck.flyOffset = approved ? 360 : -360
            deck.flyRotation = approved ? 12 : -12
            deck.flyOpacity = 0
        }

        Task { @MainActor in
            try? await Task.sleep(for: flight)
            deck.finishDecision()
            clampIndex()
        }
    }
}

private struct PendingBatch: Identifiable {
    let id: String
    let approval: Approval
    let items: [Approval]
    let failed: [Approval]
}

/// A peek card that sits behind the front card, establishing depth.
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
                    .background(Paper.ink.opacity(0.06), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .foregroundStyle(Paper.ink)
                Text(approval.title)
                    .font(.system(size: 12, weight: .semibold))
                    .lineLimit(1)
                    .foregroundStyle(Paper.ink)
                Spacer()
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(maxWidth: .infinity)
        .frame(height: 240)
        .background(Paper.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(Paper.hairline, lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.06), radius: 4, y: 2)
        .scaleEffect(scale)
        .offset(y: yOffset)
        .opacity(opacity)
        .zIndex(Double(5 - offset))
        .animation(.spring(response: 0.35, dampingFraction: 0.75), value: isTopAdvancing)
        .allowsHitTesting(false)
    }
}

struct EmailBody: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(EmailParagraph.parse(text)) { paragraph in
                Text(verbatim: paragraph.text)
                    .font(.system(size: 13))
                    .lineSpacing(3)
                    .foregroundStyle(paragraph.quoted ? Paper.ink.opacity(0.7) : Paper.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.leading, paragraph.quoted ? 10 : 0)
                    .overlay(alignment: .leading) {
                        if paragraph.quoted {
                            Rectangle().fill(Paper.hairline).frame(width: 2)
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
                    .font(.system(size: 13, weight: .semibold))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if email.messages.count > 1 {
                Picker("Email in thread", selection: $selection.message) {
                    ForEach(email.messages.indices, id: \.self) { index in
                        Text("\(index + 1). \(email.messages[index].sender ?? "Email")").tag(index)
                    }
                }
                .pickerStyle(.menu)
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
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                if let body = message.body {
                    EmailBody(text: body)
                } else {
                    Text("Email body unavailable").font(.callout).foregroundStyle(.secondary)
                }
            }
            if let reason = email.unavailableReason {
                Text(reason).font(.callout).foregroundStyle(.secondary)
            }
        }
    }
}