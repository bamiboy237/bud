import Foundation
import SwiftUI

struct DesktopDeliverySelection: Identifiable {
    let connection: BudConnection
    let session: String
    let jobID: String
    var id: String { jobID }
    var path: String { "sessions/\(session)/desktop-jobs/\(jobID)" }
}

@MainActor
struct DesktopDeliveryInspector: View {
    let selection: DesktopDeliverySelection
    let close: () -> Void
    @State private var file = ""
    @State private var url = ""
    @State private var desktop = ""
    @State private var desktopID = ""
    @State private var reference = ""
    @State private var phase = ""
    @State private var problem: String?
    @State private var working = false
    @State private var desktopChoices: [String: String] = [:]
    @State private var targetID = ""
    @State private var loadVersion = 0

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
                    if !phase.isEmpty {
                        Text(summary).font(Paper.Typography.callout)
                        LabeledContent("File", value: file)
                        LabeledContent("Desktop", value: desktop)
                        Text(url).font(.caption).textSelection(.enabled)
                        if phase == "delivered" || phase == "uncertain" {
                            Text("Reference \(reference)").font(.caption).textSelection(.enabled)
                        }
                        if phase == "ready" {
                            Button("Approve submission") {
                                Task { await act("approve", body: ["desktop": desktopID]) }
                            }
                                .disabled(working || desktopID.isEmpty)
                        }
                        if canChange || phase == "approved" {
                            if canChange, !desktopChoices.isEmpty {
                                Picker("Run browser on", selection: $targetID) {
                                    Text(desktop).tag("")
                                    ForEach(desktopChoices.keys.sorted(), id: \.self) { id in
                                        if id != desktopID { Text(desktopChoices[id] ?? id).tag(id) }
                                    }
                                }
                                Button("Use selected desktop") {
                                    Task { await act("retarget", body: ["desktop": targetID]) }
                                }
                                .disabled(working || targetID.isEmpty)
                            }
                            Button("Cancel delivery") { Task { await act("cancel") } }
                                .disabled(working)
                        }
                        if phase == "uncertain" {
                            Button("Check for receipt") { Task { await act("verify") } }
                                .disabled(working)
                        }
                    } else if problem == nil {
                        ProgressView().controlSize(.small)
                    }
                    if let problem { Text(problem).foregroundStyle(Paper.errorInk) }
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
                if phase == "delivered" || phase == "cancelled" { break }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private var canChange: Bool { ["queued", "sign_in", "ready"].contains(phase) }

    private var summary: String {
        switch phase {
        case "queued": "Waiting for \(desktop)"
        case "sign_in": "Finish private sign-in on \(desktop), then choose Continue in Chrome."
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
            if let id = row["id"], let label = row["label"] { desktopChoices[id] = label }
        }
    }

    private func load() async {
        guard !working else { return }
        let version = loadVersion
        do {
            let status = try await selection.connection.send("GET", selection.path)
            guard version == loadVersion else { return }
            if let form = status["browser"] as? [String: String] { url = form["url"] ?? "" }
            if let attachment = status["file"] as? [String: String] { file = attachment["name"] ?? "" }
            if let steps = status["steps"] as? [[String: Any]], let step = steps.last {
                phase = step["browser_state"] as? String ?? ""
                desktop = step["desktop"] as? String ?? ""
                desktopID = step["desktop_id"] as? String ?? ""
                reference = step["id"] as? String ?? ""
            }
        } catch {
            if version == loadVersion { problem = "\(error)" }
        }
    }

    private func act(_ action: String, body: [String: String]? = nil) async {
        guard !working else { return }
        working = true
        loadVersion += 1
        problem = nil
        do {
            _ = try await selection.connection.send(
                "POST", "\(selection.path)/browser/\(action)", json: body)
            targetID = ""
        } catch { problem = "\(error)" }
        working = false
        await load()
    }
}
