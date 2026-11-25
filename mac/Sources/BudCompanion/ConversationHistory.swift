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
        VStack(alignment: .leading, spacing: 12) {
            Text("Conversations").font(Paper.Typography.h2).padding(.horizontal, 20)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(state.entries) { entry in
                        Button {
                            if let url = entry.link { show(url) }
                        } label: {
                            HStack(alignment: .firstTextBaseline, spacing: 12) {
                                Text(entry.label).lineLimit(2)
                                Spacer()
                                Text(entry.updated.prefix(10)).foregroundStyle(Paper.inkSecondary)
                            }
                            .padding(.vertical, 10)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        Divider()
                    }
                    if state.loading {
                        ProgressView().controlSize(.small).padding(.top, 12)
                    } else if state.failed {
                        Button("Try Again") { Task { await state.load(offset: state.entries.count) } }
                    } else if state.entries.isEmpty {
                        Text("No conversations yet").foregroundStyle(Paper.inkSecondary)
                    } else if state.more {
                        Button("Show more") { Task { await state.load(offset: state.entries.count) } }
                            .padding(.top, 12)
                    }
                }
                .padding(.horizontal, 20)
            }
            Divider()
            HStack {
                Spacer()
                Button("Done", action: close).keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 20)
        }
        .padding(.vertical, 18)
        .frame(width: 460, height: 350)
        .background(Paper.canvas)
        .task { await state.load(offset: 0) }
    }
}
