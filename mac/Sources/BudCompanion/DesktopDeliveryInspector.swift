import Foundation
import SwiftUI

struct DesktopDeliverySelection: Identifiable {
    let connection: BudConnection
    let session: String
    let jobID: String
    let id = UUID()
    var path: String { "sessions/\(session)/desktop-jobs/\(jobID)" }
}

struct DesktopDeliveryStatus {
    let file: ChatAttachment
    let url: String
    let desktop: String
    let desktopID: String
    let reference: String
    let phase: String
    let revoked: Bool

    init?(_ payload: [String: Any]) {
        guard let form = payload["browser"] as? [String: String], let url = form["url"],
              let attachment = payload["file"] as? [String: String],
              let fileID = attachment["id"], let filename = attachment["name"],
              let step = (payload["steps"] as? [[String: Any]])?.last,
              let desktop = step["desktop"] as? String, let desktopID = step["desktop_id"] as? String,
              let reference = step["id"] as? String, let phase = step["browser_state"] as? String,
              let revoked = step["revoked"] as? Bool,
              !url.isEmpty, !fileID.isEmpty, !filename.isEmpty, !desktopID.isEmpty
        else { return nil }
        file = ChatAttachment(id: fileID, name: filename)
        self.url = url
        self.desktop = desktop
        self.desktopID = desktopID
        self.reference = reference
        self.phase = phase
        self.revoked = revoked
    }

    var canApprove: Bool { phase == "ready" && !revoked }
    var canChange: Bool { ["queued", "sign_in", "ready"].contains(phase) }
}

@MainActor
private final class DesktopDeliveryState: ObservableObject {
    @Published var status: DesktopDeliveryStatus?
    @Published var preview: AttachmentSelection?
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
                Text("Browser delivery")
                    .font(Paper.Typography.label)
                    .foregroundStyle(Paper.inkPrimary)
                Spacer()
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(IconButton(size: 26))
                    .accessibilityLabel("Close browser delivery")
            }
            .font(.system(size: 12, weight: .medium))
            .padding(.horizontal, 14)
            .frame(height: 40)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let status = state.status {
                        Text(summary(status)).font(Paper.Typography.callout)
                        Rectangle().fill(Paper.hairline).frame(height: 0.5)
                        LabeledContent("File", value: status.file.name)
                        Button("Preview selected file") {
                            state.preview = AttachmentSelection(
                                file: status.file, session: selection.session,
                                connection: selection.connection)
                        }
                        .buttonStyle(CardButton(kind: .secondary))
                        Rectangle().fill(Paper.hairline).frame(height: 0.5)
                        LabeledContent("Desktop", value: desktopLabel(status.desktop, id: status.desktopID))
                        Text(status.url)
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkSecondary)
                            .textSelection(.enabled)
                        if status.phase == "delivered" || status.phase == "uncertain" {
                            Text("Reference \(status.reference)")
                                .font(Paper.Typography.caption)
                                .foregroundStyle(Paper.inkSecondary)
                                .textSelection(.enabled)
                        }
                        if status.canApprove {
                            Button("Approve submission") {
                                Task { await act("approve", body: ["desktop": status.desktopID]) }
                            }
                            .buttonStyle(CardButton(kind: .primary))
                            .disabled(state.working)
                        }
                        if status.canChange || status.phase == "approved" {
                            if status.canChange, !state.desktopChoices.isEmpty {
                                Picker("Run browser on", selection: $state.targetID) {
                                    Text(desktopLabel(status.desktop, id: status.desktopID)).tag("")
                                    ForEach(state.desktopChoices.keys.sorted(), id: \.self) { id in
                                        if let name = state.desktopChoices[id], id != status.desktopID {
                                            Text(desktopLabel(name, id: id)).tag(id)
                                        }
                                    }
                                }
                                Button("Use selected desktop") {
                                    let targetID = state.targetID
                                    Task { await act("retarget", body: ["desktop": targetID]) }
                                }
                                .buttonStyle(CardButton(kind: .secondary))
                                .disabled(state.working || state.targetID.isEmpty)
                                .opacity(state.working || state.targetID.isEmpty ? 0.55 : 1)
                            }
                            Button("Cancel delivery") { Task { await act("cancel") } }
                                .buttonStyle(CardButton(kind: .quiet))
                                .disabled(state.working)
                        }
                        if status.phase == "uncertain" {
                            Button("Check for receipt") { Task { await act("verify") } }
                                .buttonStyle(CardButton(kind: .secondary))
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
        .sheet(item: $state.preview) { selected in
            AttachmentInspector(selection: selected, close: { state.preview = nil })
                .frame(minWidth: 460, minHeight: 420)
        }
        .task {
            await loadDesktops()
            while !Task.isCancelled {
                await load()
                if state.status?.phase == "delivered" || state.status?.phase == "cancelled" { break }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private func desktopLabel(_ name: String, id: String) -> String {
        "\(name) · \(id.prefix(8))"
    }

    private func summary(_ status: DesktopDeliveryStatus) -> String {
        switch status.phase {
        case "queued": "Waiting for \(status.desktop)"
        case "sign_in": "Finish private sign-in on \(status.desktop), then choose Continue in the browser."
        case "ready": status.revoked ? "Choose another desktop." : "Review the file and site before approving."
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
            guard version == state.loadVersion, !Task.isCancelled else { return }
            let next = DesktopDeliveryStatus(status)
            if state.status?.desktopID != next?.desktopID {
                state.targetID = ""
            }
            state.status = next
            state.problem = next == nil ? "Couldn’t load the delivery details." : nil
        } catch {
            guard version == state.loadVersion, !Task.isCancelled else { return }
            state.status = nil
            state.problem = "\(error)"
        }
    }

    private func act(_ action: String, body: [String: String]? = nil) async {
        guard !state.working else { return }
        state.working = true
        state.loadVersion += 1
        state.status = nil
        state.problem = nil
        do {
            _ = try await selection.connection.send(
                "POST", "\(selection.path)/browser/\(action)", json: body)
            state.targetID = ""
        } catch {
            state.problem = "\(error)"
            state.working = false
            return
        }
        state.working = false
        await load()
    }
}
