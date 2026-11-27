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
    var awaitsReceipt: Bool { phase == "approved" || phase == "sending" }
    var receiptUnconfirmed: Bool { phase == "uncertain" }
}

enum DesktopDeliveryAction: String, Sendable {
    case approve, cancel, retarget, verify

    var title: String {
        switch self {
        case .approve: "Approve submission"
        case .cancel: "Cancel delivery"
        case .retarget: "Use selected desktop"
        case .verify: "Check for receipt"
        }
    }

    var progressTitle: String {
        switch self {
        case .approve: "Approving…"
        case .cancel: "Cancelling…"
        case .retarget: "Changing desktop…"
        case .verify: "Checking for receipt…"
        }
    }
}

@MainActor
final class DesktopDeliveryState: ObservableObject {
    @Published private(set) var status: DesktopDeliveryStatus?
    @Published var preview: AttachmentSelection?
    @Published private var actionProblem: String?
    @Published private var loadProblem: String?
    @Published private(set) var action: DesktopDeliveryAction?
    @Published var desktopChoices: [String: String] = [:]
    @Published var targetID = ""
    private var loadVersion = 0

    var problem: String? { actionProblem ?? loadProblem }
    var canCheckReceipt: Bool {
        status?.receiptUnconfirmed == true
            || (status?.awaitsReceipt == true && (loadProblem != nil || action == .verify))
    }

    func refresh(_ selection: DesktopDeliverySelection) async {
        guard action == nil else { return }
        await load(selection)
    }

    func perform(
        _ action: DesktopDeliveryAction, on selection: DesktopDeliverySelection, desktopID: String? = nil
    ) async {
        guard self.action == nil else { return }
        self.action = action
        loadVersion += 1
        actionProblem = nil
        loadProblem = nil
        defer { self.action = nil }

        do {
            // Only an uncertain job accepts the server's read-only receipt check.
            // If its status could not be loaded, ask for the latest state first.
            if action != .verify || status?.receiptUnconfirmed == true {
                let request = selection.connection.request(
                    "POST", "\(selection.path)/browser/\(action.rawValue)",
                    json: desktopID.map { ["desktop": $0] })
                _ = try await selection.connection.send(request)
                targetID = ""
            }
            await load(selection)
        } catch {
            actionProblem = "\(error)"
        }
    }

    private func load(_ selection: DesktopDeliverySelection) async {
        let version = loadVersion
        do {
            let payload = try await selection.connection.send("GET", selection.path)
            guard version == loadVersion, !Task.isCancelled else { return }
            guard let next = DesktopDeliveryStatus(payload) else {
                loadProblem = "Couldn’t load the delivery details."
                return
            }
            let desktopChanged = status?.desktopID != next.desktopID
            if desktopChanged { targetID = "" }
            if desktopChanged || status?.phase != next.phase {
                actionProblem = nil
            }
            status = next
            loadProblem = nil
        } catch {
            guard version == loadVersion, !Task.isCancelled else { return }
            loadProblem = "\(error)"
        }
    }
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
                        if status.phase == "delivered" || status.receiptUnconfirmed {
                            Text("Reference \(status.reference)")
                                .font(Paper.Typography.caption)
                                .foregroundStyle(Paper.inkSecondary)
                                .textSelection(.enabled)
                        }
                        Rectangle().fill(Paper.hairline).frame(height: 0.5)
                        actions(for: status)
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
                await state.refresh(selection)
                if state.status?.phase == "delivered" || state.status?.phase == "cancelled" { break }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private func actions(for status: DesktopDeliveryStatus) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(state.canCheckReceipt ? "Receipt unconfirmed" : summary(status))
                    .font(Paper.Typography.callout)
                if state.canCheckReceipt {
                    actionButton(.verify, kind: .secondary)
                }
            }
            if status.canApprove {
                actionButton(.approve, kind: .primary, desktopID: status.desktopID)
            }
            if status.canChange, !state.desktopChoices.isEmpty {
                Picker("Run browser on", selection: $state.targetID) {
                    Text(desktopLabel(status.desktop, id: status.desktopID)).tag("")
                    ForEach(state.desktopChoices.keys.sorted(), id: \.self) { id in
                        if let name = state.desktopChoices[id], id != status.desktopID {
                            Text(desktopLabel(name, id: id)).tag(id)
                        }
                    }
                }
                .disabled(state.action != nil)
                actionButton(
                    .retarget, kind: .secondary, desktopID: state.targetID, enabled: !state.targetID.isEmpty)
            }
            if status.canChange || status.phase == "approved" {
                actionButton(.cancel, kind: .quiet)
            }
        }
    }

    @ViewBuilder private func actionButton(
        _ action: DesktopDeliveryAction, kind: CardButton.Kind,
        desktopID: String? = nil, enabled: Bool = true
    ) -> some View {
        if state.action == action {
            ProgressView(action.progressTitle)
                .controlSize(.small)
                .font(Paper.Typography.label)
                .frame(minHeight: 26)
        } else {
            Button(action.title) {
                Task { await state.perform(action, on: selection, desktopID: desktopID) }
            }
            .buttonStyle(CardButton(kind: kind))
            .disabled(state.action != nil || !enabled)
            .opacity(enabled ? 1 : 0.55)
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
}
