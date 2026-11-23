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
struct DesktopHandoffPicker: View {
    let selection: AttachmentSelection
    @State private var desktops: [PairedDesktop]?
    @State private var chosen: [String] = []
    @State private var problem: String?
    @State private var dispatching = false
    @State private var queued = false
    @State private var jobID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Verify on paired desktops").font(Paper.Typography.body)
            if queued {
                Label("Queued. Results will appear in this chat.", systemImage: "checkmark.circle")
            } else if let desktops {
                if desktops.isEmpty {
                    Text("Pair a desktop first with bud desktop pair.")
                        .foregroundStyle(Paper.inkSecondary)
                } else {
                    Text("Choose up to two desktops in the order they should run.")
                        .foregroundStyle(Paper.inkSecondary)
                    ForEach(desktops) { desktop in
                        Button {
                            if chosen.contains(desktop.id) {
                                chosen.removeAll { $0 == desktop.id }
                            } else if chosen.count < 2 {
                                chosen.append(desktop.id)
                            }
                            jobID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
                        } label: {
                            HStack {
                                Image(systemName: chosen.contains(desktop.id)
                                    ? "checkmark.circle.fill" : "circle")
                                Text(desktop.label)
                                if let order = chosen.firstIndex(of: desktop.id) {
                                    Text("\(order + 1)").foregroundStyle(Paper.inkSecondary)
                                }
                            }
                        }
                        .buttonStyle(.plain)
                        .disabled(dispatching)
                    }
                    Button("Queue verification") { Task { await dispatch() } }
                        .disabled(chosen.isEmpty || dispatching)
                }
            } else if problem == nil {
                ProgressView().controlSize(.small)
            }
            if let problem { Text(problem).foregroundStyle(Paper.inkSecondary) }
        }
        .frame(width: 280, alignment: .leading)
        .task { await load() }
    }

    private func load() async {
        do {
            let result = try await selection.connection.getBytes("desktops", queryItems: [])
            let entries = try JSONSerialization.jsonObject(with: result) as? [[String: Any]] ?? []
            desktops = entries.compactMap(PairedDesktop.init)
        } catch {
            problem = "Couldn’t load the paired desktops."
        }
    }

    private func dispatch() async {
        guard !dispatching else { return }
        dispatching = true
        problem = nil
        defer { dispatching = false }
        do {
            _ = try await selection.connection.send(
                "POST", "sessions/\(selection.session)/desktop-jobs",
                json: ["job_id": jobID, "file": selection.file.fields, "desktops": chosen])
            queued = true
        } catch {
            problem = "\(error)"
        }
    }
}
