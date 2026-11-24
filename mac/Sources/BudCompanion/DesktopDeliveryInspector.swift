import Foundation
import SwiftUI

struct DesktopDeliverySelection: Identifiable {
    let connection: BudConnection
    let session: String
    let jobID: String
    let id = UUID()
    var path: String { "sessions/\(session)/desktop-jobs/\(jobID)" }
}

@MainActor
private final class DesktopDeliveryState: ObservableObject {
    @Published var file = ""
    @Published var url = ""
    @Published var desktop = ""
    @Published var desktopID = ""
    @Published var reference = ""
    @Published var phase = ""
    @Published var problem: String?
    @Published var working = false
    @Published var desktopChoices: [String: String] = [:]
    @Published var targetID = ""
    var loadVersion = 0
}

@MainActor
struct DesktopDeliveryInspector: View {
    let selection: DesktopDeliverySelection
    let close: () -> Void
    @StateObject private var state = DesktopDeliveryState()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Browser delivery").font(Paper.Typography.callout)
                Spacer()
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(IconButton(size: 24))
                    .accessibilityLabel("Close browser delivery")
            }.padding(12)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if !state.phase.isEmpty {
                        Text(summary).font(Paper.Typography.callout)
                        LabeledContent("File", value: state.file)
                        LabeledContent("Desktop", value: state.desktop)
                        Text(state.url).font(.caption).textSelection(.enabled)
                        if state.phase == "delivered" || state.phase == "uncertain" {
                            Text("Reference \(state.reference)").font(.caption).textSelection(.enabled)
                        }
                        if state.phase == "ready" {
                            Button("Approve submission") {
                                let desktopID = state.desktopID
                                Task { await act("approve", body: ["desktop": desktopID]) }
                            }
                            .disabled(state.working || state.desktopID.isEmpty)
                        }
                        if canChange || state.phase == "approved" {
                            if canChange, !state.desktopChoices.isEmpty {
                                Picker("Run browser on", selection: $state.targetID) {
                                    Text(state.desktop).tag("")
                                    ForEach(state.desktopChoices.keys.sorted(), id: \.self) { id in
                                        if id != state.desktopID {
                                            Text(state.desktopChoices[id] ?? id).tag(id)
                                        }
                                    }
                                }
                                Button("Use selected desktop") {
                                    let targetID = state.targetID
                                    Task { await act("retarget", body: ["desktop": targetID]) }
                                }
                                .disabled(state.working || state.targetID.isEmpty)
                            }
                            Button("Cancel delivery") { Task { await act("cancel") } }
                                .disabled(state.working)
                        }
                        if state.phase == "uncertain" {
                            Button("Check for receipt") { Task { await act("verify") } }
                                .disabled(state.working)
                        }
                    } else if state.problem == nil {
                        ProgressView().controlSize(.small)
                    }
                    if let problem = state.problem { Text(problem).foregroundStyle(Paper.errorInk) }
                }
                .foregroundStyle(Paper.inkPrimary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
            }
        }
        .background(Paper.canvas)
        .inspectorColumnWidth(min: 270, ideal: 340, max: 480)
        .task {
            await loadDesktops()
            while !Task.isCancelled {
                await load()
                if state.phase == "delivered" || state.phase == "cancelled" { break }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private var canChange: Bool { ["queued", "sign_in", "ready"].contains(state.phase) }

    private var summary: String {
        switch state.phase {
        case "queued": "Waiting for \(state.desktop)"
        case "sign_in": "Finish private sign-in on \(state.desktop), then choose Continue in the browser."
        case "ready": "Review the file and site before approving."
        case "approved", "sending": "Waiting for the browser receipt"
        case "delivered": "Receipt confirmed"
        case "uncertain": "Bud couldn’t confirm submission. You can check again for a receipt."
        case "cancelled": "Delivery cancelled"
        default: "Browser delivery"
        }
    }

    private func loadDesktops() async {
        guard let data = try? await selection.connection.getBytes("desktops", queryItems: []),
              let rows = (try? JSONSerialization.jsonObject(with: data)) as? [[String: String]]
        else { return }
        for row in rows {
            if let id = row["id"], let label = row["label"] { state.desktopChoices[id] = label }
        }
    }

    private func load() async {
        guard !state.working else { return }
        let version = state.loadVersion
        do {
            let status = try await selection.connection.send("GET", selection.path)
            guard version == state.loadVersion else { return }
            if let form = status["browser"] as? [String: String] {
                state.url = form["url"] ?? ""
            }
            if let attachment = status["file"] as? [String: String] {
                state.file = attachment["name"] ?? ""
            }
            if let steps = status["steps"] as? [[String: Any]], let step = steps.last {
                state.phase = step["browser_state"] as? String ?? ""
                state.desktop = step["desktop"] as? String ?? ""
                state.desktopID = step["desktop_id"] as? String ?? ""
                state.reference = step["id"] as? String ?? ""
            }
        } catch {
            if version == state.loadVersion { state.problem = "\(error)" }
        }
    }

    private func act(_ action: String, body: [String: String]? = nil) async {
        guard !state.working else { return }
        state.working = true
        state.loadVersion += 1
        state.problem = nil
        do {
            _ = try await selection.connection.send(
                "POST", "\(selection.path)/browser/\(action)", json: body)
            state.targetID = ""
        } catch { state.problem = "\(error)" }
        state.working = false
        await load()
    }
}
