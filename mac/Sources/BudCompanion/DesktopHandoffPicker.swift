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
    @Published var useBrowser = false
    @Published var formURL = ""
    @Published var fileID = ""
    @Published var referenceID = ""
    @Published var submitID = ""
    @Published var receiptID = ""
    @Published var jobID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
}

@MainActor
struct DesktopHandoffPicker: View {
    let selection: AttachmentSelection
    @StateObject private var state = DesktopHandoffState()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Send to paired desktops").font(Paper.Typography.body)
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
                            resetJob()
                        } label: {
                            HStack {
                                Image(systemName: state.chosen.contains(desktop.id)
                                    ? "checkmark.circle.fill" : "circle")
                                Text(desktop.label)
                                if let order = state.chosen.firstIndex(of: desktop.id) {
                                    Text("\(order + 1)").foregroundStyle(Paper.inkSecondary)
                                }
                            }
                            .padding(6)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .buttonStyle(PaperHit(shape: RoundedRectangle(cornerRadius: Paper.radiusSM)))
                        .disabled(state.dispatching)
                        .accessibilityAddTraits(state.chosen.contains(desktop.id) ? .isSelected : [])
                    }
                    DisclosureGroup("Deliver through a website", isExpanded: $state.useBrowser) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("The last desktop opens the form. You approve before it sends the file.")
                                .font(.caption).foregroundStyle(Paper.inkSecondary)
                            TextField("Form URL", text: $state.formURL)
                            TextField("File input ID", text: $state.fileID)
                            TextField("Reference field ID", text: $state.referenceID)
                            TextField("Submit button ID", text: $state.submitID)
                            TextField("Receipt element ID", text: $state.receiptID)
                        }
                        .padding(.top, 4)
                    }
                    .disabled(state.dispatching)
                    Button(state.useBrowser ? "Queue browser delivery" : "Queue verification") {
                        Task { await dispatch() }
                    }
                    .buttonStyle(CardButton(kind: .primary))
                    .disabled(!canDispatch)
                    .opacity(canDispatch ? 1 : 0.45)
                }
            } else if state.problem == nil {
                ProgressView().controlSize(.small)
            }
            if let problem = state.problem { Text(problem).foregroundStyle(Paper.inkSecondary) }
        }
        .frame(width: 280, alignment: .leading)
        .task { await load() }
        .onChange(of: browserForm) { _, _ in resetJob() }
        .onChange(of: state.useBrowser) { _, _ in resetJob() }
    }

    private var browserForm: [String: String] {
        [
            "url": state.formURL, "file_input": state.fileID, "reference_input": state.referenceID,
            "submit_button": state.submitID, "receipt": state.receiptID,
        ].mapValues { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
    }

    private var canDispatch: Bool {
        !state.chosen.isEmpty && !state.dispatching
            && (!state.useBrowser || !browserForm.values.contains(""))
    }

    private func resetJob() {
        state.jobID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
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
        guard canDispatch else { return }
        state.dispatching = true
        state.problem = nil
        defer { state.dispatching = false }
        do {
            var payload: [String: Any] = [
                "job_id": state.jobID, "file": selection.file.fields, "desktops": state.chosen,
            ]
            if state.useBrowser { payload["browser"] = browserForm }
            _ = try await selection.connection.send(
                "POST", "sessions/\(selection.session)/desktop-jobs", json: payload)
            state.queued = true
        } catch {
            state.problem = "\(error)"
        }
    }
}
