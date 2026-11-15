import SwiftUI

final class ApprovalLayout: ObservableObject {
    @Published var height: CGFloat = 1
    @Published var message = 0
}

/// Which pending rows are open. One pending action opens by default; with several, each row
/// starts closed so the list stays short, and a click opens or closes that row.
final class ApprovalExpansion: ObservableObject {
    @Published private(set) var open: Set<String> = []
    private var ids: [String] = []

    func sync(_ current: [String]) {
        guard current != ids else { return }
        let first = ids.isEmpty
        ids = current
        open.formIntersection(current)
        if first, current.count == 1, let only = current.first { open = [only] }
    }

    func toggle(_ id: String) {
        if open.contains(id) { open.remove(id) } else { open.insert(id) }
    }
}

/// Every pending action as its own row. A row shows what it will do and opens to the full
/// request, so several waiting actions stay scannable instead of stacking into one tall card.
struct ApprovalDeck: View {
    @Bindable var companion: Companion
    @StateObject private var expansion = ApprovalExpansion()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(companion.approvals) { approval in
                ApprovalRow(companion: companion, approval: approval,
                            open: expansion.open.contains(approval.id)) {
                    expansion.toggle(approval.id)
                }
                .id(approval.id)
            }
            ForEach(batches) { batch in
                batchButton(batch)
            }
        }
        .onAppear { expansion.sync(companion.approvals.map(\.id)) }
        .onChange(of: companion.approvals.map(\.id)) { _, ids in expansion.sync(ids) }
        .animation(.spring(duration: 0.25, bounce: 0), value: expansion.open)
    }

    /// One button per request that has more than one action waiting. A new request never
    /// joins an older one, so a button always names the actions it will approve.
    private var batches: [PendingBatch] {
        var seen: Set<String> = []
        var found: [PendingBatch] = []
        for approval in companion.approvals {
            guard let key = approval.requestID, !seen.contains(key) else { continue }
            let items = approval.batch(in: companion.approvals)
            guard items.count > 1 else { continue }
            seen.insert(key)
            found.append(
                PendingBatch(
                    id: key,
                    approval: approval,
                    items: items,
                    failed: approval.retryBatch(in: companion.approvals, failures: companion.approvalFailures)
                )
            )
        }
        return found
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
            HStack {
                Text(companion.approvingBatch ? "Approving…" :
                        failed.isEmpty ? "Approve all (\(batch.items.count))" : "Retry failed (\(failed.count))")
                Spacer()
                Image(systemName: "checkmark.circle")
            }
        }
        .buttonStyle(CardButton(kind: .secondary))
        .disabled(!companion.deciding.isEmpty || companion.approvingBatch || companion.link != .connected)
        .help(failed.isEmpty ? "Approve pending actions from this request only" : "Retry only failed approvals that are still pending")
    }
}

private struct PendingBatch: Identifiable {
    let id: String
    let approval: Approval
    let items: [Approval]
    let failed: [Approval]
}

/// One pending action: a summary line that opens and closes, and the full card when open.
struct ApprovalRow: View {
    let companion: Companion
    let approval: Approval
    let open: Bool
    let toggle: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button(action: toggle) {
                HStack(alignment: .center, spacing: 10) {
                    Image(systemName: approval.symbol)
                        .font(.system(size: 12, weight: .medium))
                        .frame(width: 26, height: 26)
                        .background(Paper.ink.opacity(0.07),
                                    in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(approval.isQuestion ? "bud has a question" : approval.title)
                            .font(.system(size: 13, weight: .semibold))
                            .lineLimit(2)
                            .multilineTextAlignment(.leading)
                        if let detail = approval.rowDetail {
                            Text(detail)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                    Spacer(minLength: 6)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .rotationEffect(.degrees(open ? 180 : 0))
                        .accessibilityHidden(true)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(approval.isQuestion ? "bud's question" : approval.title)
            .accessibilityHint(open ? "Collapse this request" : "Expand this request")
            if open {
                ApprovalCard(companion: companion, approval: approval, nested: true, showsHeading: false)
                    .transition(.opacity)
            }
        }
        .padding(12)
        .background(Paper.ink.opacity(0.035),
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .strokeBorder(Paper.hairline, lineWidth: 0.5))
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
