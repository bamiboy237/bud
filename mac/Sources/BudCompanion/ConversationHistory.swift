import SwiftUI

private struct ConversationSummary: Decodable, Identifiable {
    let id: String
    let seq: Int
    let updated: String
    let title: String?

    enum CodingKeys: String, CodingKey {
        case id = "session_id"
        case seq, updated, title
    }

    var label: String {
        let text = (title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? "Attachment" : text
    }

    var link: URL? {
        var url = URLComponents()
        url.scheme = Citations.scheme
        url.host = id
        url.path = "/\(seq)"
        return url.url
    }
}

@MainActor @Observable
private final class ConversationHistoryState: ObservableObject {
    var entries: [ConversationSummary] = []
    var loading = false
    var more = false
    var failed = false

    func load(offset: Int) async {
        guard !loading else { return }
        loading = true
        failed = false
        defer { loading = false }
        do {
            let data = try await Companion.shared.conversationHistory(offset: offset)
            let page = try JSONDecoder().decode([ConversationSummary].self, from: data)
            entries.append(contentsOf: page)
            more = page.count == 50
        } catch {
            failed = true
        }
    }
}

struct ConversationHistory: View {
    @StateObject private var state = ConversationHistoryState()
    let show: (URL) -> Void
    let close: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Conversations")
                .font(Paper.Typography.label)
                .foregroundStyle(Paper.inkPrimary)
                .padding(.horizontal, 14)
                .frame(height: 40)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(state.entries) { entry in
                        Button {
                            if let url = entry.link { show(url) }
                        } label: {
                            HStack(alignment: .firstTextBaseline, spacing: 12) {
                                Text(entry.label)
                                    .font(Paper.Typography.callout)
                                    .foregroundStyle(Paper.inkPrimary)
                                    .lineLimit(2)
                                Spacer()
                                Text(entry.updated.prefix(10))
                                    .font(Paper.Typography.ledgerMeta)
                                    .foregroundStyle(Paper.inkMuted)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 10)
                        }
                        .buttonStyle(PaperHit(shape: RoundedRectangle(cornerRadius: Paper.radiusSM)))
                        Rectangle().fill(Paper.hairline).frame(height: 0.5)
                    }
                    if state.loading {
                        ProgressView().controlSize(.small).padding(.top, 12)
                    } else if state.failed {
                        Button("Try Again") { Task { await state.load(offset: state.entries.count) } }
                            .buttonStyle(CardButton(kind: .secondary))
                            .padding(.top, 12)
                    } else if state.entries.isEmpty {
                        Text("No conversations yet")
                            .font(Paper.Typography.callout)
                            .foregroundStyle(Paper.inkSecondary)
                            .padding(.top, 12)
                    } else if state.more {
                        Button("Show more") { Task { await state.load(offset: state.entries.count) } }
                            .buttonStyle(CardButton(kind: .secondary))
                            .padding(.top, 12)
                    }
                }
                .padding(.horizontal, 14)
            }
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            HStack {
                Spacer()
                Button("Done", action: close)
                    .buttonStyle(CardButton(kind: .primary))
                    .keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
        }
        .frame(width: 460, height: 350)
        .background(Paper.canvas)
        .task { await state.load(offset: 0) }
    }
}
