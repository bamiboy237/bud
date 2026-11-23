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
    @State private var useBrowser = false
    @State private var formURL = ""
    @State private var fileID = ""
    @State private var referenceID = ""
    @State private var submitID = ""
    @State private var receiptID = ""
    @State private var jobID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Send to paired desktops").font(Paper.Typography.body)
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
                            resetJob()
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
                    DisclosureGroup("Deliver through a website", isExpanded: $useBrowser) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("The last desktop opens the form. You approve before it sends the file.")
                                .font(.caption).foregroundStyle(Paper.inkSecondary)
                            TextField("Form URL", text: $formURL)
                            TextField("File input ID", text: $fileID)
                            TextField("Reference field ID", text: $referenceID)
                            TextField("Submit button ID", text: $submitID)
                            TextField("Receipt element ID", text: $receiptID)
                        }
                        .padding(.top, 4)
                    }
                    .disabled(dispatching)
                    Button(useBrowser ? "Queue browser delivery" : "Queue verification") {
                        Task { await dispatch() }
                    }
                    .disabled(chosen.isEmpty || dispatching || (useBrowser && !hasForm))
                }
            } else if problem == nil {
                ProgressView().controlSize(.small)
            }
            if let problem { Text(problem).foregroundStyle(Paper.inkSecondary) }
        }
        .frame(width: 280, alignment: .leading)
        .task { await load() }
        .onChange(of: browserForm) { _, _ in resetJob() }
        .onChange(of: useBrowser) { _, _ in resetJob() }
    }

    private var browserForm: [String: String] {
        [
            "url": formURL, "file_input": fileID, "reference_input": referenceID,
            "submit_button": submitID, "receipt": receiptID,
        ].mapValues { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
    }

    private var hasForm: Bool { !browserForm.values.contains("") }

    private func resetJob() {
        jobID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
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
            var payload: [String: Any] = [
                "job_id": jobID, "file": selection.file.fields, "desktops": chosen,
            ]
            if useBrowser { payload["browser"] = browserForm }
            _ = try await selection.connection.send(
                "POST", "sessions/\(selection.session)/desktop-jobs", json: payload)
            queued = true
        } catch {
            problem = "\(error)"
        }
    }
}
