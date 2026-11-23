import Foundation
import SwiftUI

private struct PairedDesktop: Identifiable {
    let id: String
    let label: String

    init?(_ entry: [String: Any]) {
        guard let id = entry["id"] as? String, let label = entry["label"] as? String else {
            return nil
        }
        self.id = id
        self.label = label
    }
}

@MainActor
private final class DesktopHandoffState: ObservableObject {
    @Published var desktops: [PairedDesktop]?
    @Published var chosen: [String] = []
    @Published var problem: String?
    @Published var dispatching = false
    @Published var queued = false
    @Published var jobID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
}

@MainActor
struct DesktopHandoffPicker: View {
    let selection: AttachmentSelection
    @StateObject private var state = DesktopHandoffState()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Verify on paired desktops").font(Paper.Typography.body)
            if state.queued {
                Label("Queued. Results will appear in this chat.", systemImage: "checkmark.circle")
            } else if let desktops = state.desktops {
                if desktops.isEmpty {
                    Text("Pair a desktop first with bud desktop pair.")
                        .foregroundStyle(Paper.inkSecondary)
                } else {
                    Text("Choose up to two desktops in the order they should run.")
                        .foregroundStyle(Paper.inkSecondary)
                    ForEach(desktops) { desktop in
                        Button {
                            if state.chosen.contains(desktop.id) {
                                state.chosen.removeAll { $0 == desktop.id }
                            } else if state.chosen.count < 2 {
                                state.chosen.append(desktop.id)
                            }
                            state.jobID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
                        } label: {
                            HStack {
                                Image(systemName: state.chosen.contains(desktop.id)
                                    ? "checkmark.circle.fill" : "circle")
                                Text(desktop.label)
                                if let order = state.chosen.firstIndex(of: desktop.id) {
                                    Text("\(order + 1)").foregroundStyle(Paper.inkSecondary)
                                }
                            }
                        }
                        .buttonStyle(.plain)
                        .disabled(state.dispatching)
                    }
                    Button("Queue verification") { Task { await dispatch() } }
                        .disabled(state.chosen.isEmpty || state.dispatching)
                }
            } else if state.problem == nil {
                ProgressView().controlSize(.small)
            }
            if let problem = state.problem { Text(problem).foregroundStyle(Paper.inkSecondary) }
        }
        .frame(width: 280, alignment: .leading)
        .task { await load() }
    }

    private func load() async {
        do {
            let result = try await selection.connection.getBytes("desktops", queryItems: [])
            let entries = try JSONSerialization.jsonObject(with: result) as? [[String: Any]] ?? []
            state.desktops = entries.compactMap(PairedDesktop.init)
        } catch {
            state.problem = "Couldn’t load the paired desktops."
        }
    }

    private func dispatch() async {
        guard !state.dispatching else { return }
        state.dispatching = true
        state.problem = nil
        defer { state.dispatching = false }
        do {
            _ = try await selection.connection.send(
                "POST", "sessions/\(selection.session)/desktop-jobs",
                json: ["job_id": state.jobID, "file": selection.file.fields, "desktops": state.chosen])
            state.queued = true
        } catch {
            state.problem = "\(error)"
        }
    }
}
