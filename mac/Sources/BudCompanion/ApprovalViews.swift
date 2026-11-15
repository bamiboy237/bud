import SwiftUI

final class ApprovalLayout: ObservableObject {
    @Published var height: CGFloat = 1
    @Published var message = 0
}

struct ApprovalDeck: View {
    @Bindable var companion: Companion

    var body: some View {
        if let approval = companion.selectedApproval {
            VStack(alignment: .leading, spacing: 10) {
                if companion.approvals.count > 1 {
                    Picker("Pending actions", selection: Binding(
                        get: { companion.selectedApproval?.id ?? "" },
                        set: { companion.selectedApprovalID = $0 }
                    )) {
                        ForEach(companion.approvals) { item in
                            Text(item.menuTitle).tag(item.id)
                        }
                    }
                    .pickerStyle(.menu)
                    .labelsHidden()
                    .accessibilityLabel("Pending actions")
                    Text("\(position(approval)) of \(companion.approvals.count) pending")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
                ApprovalCard(companion: companion, approval: approval)
                    .id(approval.id)
                let batch = approval.batch(in: companion.approvals)
                let failed = companion.failedApprovals(for: approval)
                if !failed.isEmpty || batch.count > 1 {
                    Button {
                        if failed.isEmpty {
                            companion.approveAll(for: approval)
                        } else {
                            companion.retryFailed(for: approval)
                        }
                    } label: {
                        HStack {
                            Text(companion.approvingBatch ? "Approving…" :
                                    failed.isEmpty ? "Approve all (\(batch.count))" : "Retry failed (\(failed.count))")
                            Spacer()
                            Image(systemName: "checkmark.circle")
                        }
                    }
                    .buttonStyle(CardButton(kind: .secondary))
                    .disabled(!companion.deciding.isEmpty || companion.approvingBatch || companion.link != .connected)
                    .help(failed.isEmpty ? "Approve pending actions from this request only" : "Retry only failed approvals that are still pending")
                }
            }
        }
    }

    private func position(_ approval: Approval) -> Int {
        (companion.approvals.firstIndex { $0.id == approval.id } ?? 0) + 1
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
