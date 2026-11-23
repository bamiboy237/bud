import AppKit
import SwiftUI

struct BrowserSelection: Identifiable {
    let connection: BudConnection
    let id = UUID()

    var local: Bool {
        let host = connection.base.host() ?? ""
        return ["127.0.0.1", "localhost", "::1", "[::1]"].contains(host)
    }
}

private struct BrowserTab: Identifiable {
    let id: String
    let title: String
    let host: String

    init?(_ raw: [String: Any]) {
        guard let id = raw["id"] as? String, let title = raw["title"] as? String else {
            return nil
        }
        self.id = id
        self.title = title
        self.host = raw["host"] as? String ?? ""
    }
}

@MainActor
private final class BrowserInspectorState: ObservableObject {
    @Published var tabs: [BrowserTab] = []
    @Published var tabID = ""
    @Published var screenshot: NSImage?
    @Published var expanded = false
    @Published var enabled: Bool?
    @Published var privateInput = false
    @Published var handoffOpen = false
    @Published var sending = false
    @Published var problem: String?
    @Published var previewIssue: String?
}

@MainActor
struct BrowserInspector: View {
    let selection: BrowserSelection
    let close: () -> Void
    @StateObject private var state = BrowserInspectorState()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Browser").font(Paper.Typography.callout)
                    Text(selection.local ? "Your Chrome on this Mac" : "Chrome on the Bud server")
                        .font(Paper.Typography.ledgerMeta).foregroundStyle(Paper.inkSecondary)
                }
                Spacer()
                Button { state.expanded.toggle() } label: {
                    Image(systemName: state.expanded
                        ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                }
                .buttonStyle(IconButton(size: 24))
                .accessibilityLabel(state.expanded ? "Narrow browser" : "Expand browser")
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(IconButton(size: 24))
                    .accessibilityLabel("Close browser")
            }
            .padding(12)
            Divider()
            Group {
                if state.privateInput {
                    VStack(alignment: .leading, spacing: 12) {
                        Label("Private input", systemImage: "lock.fill")
                            .font(Paper.Typography.callout)
                        Text("Bud’s browser actions and preview are paused.")
                        if state.handoffOpen {
                            Text("Finish in Chrome, then select Continue in the tab.")
                        } else if !state.sending {
                            Text("Finish private input in Chrome before resuming.")
                            Button("Resume Bud") { Task { await send("resume") } }
                        }
                    }
                } else if state.enabled == false {
                    Text("Connect and enable Browse from chat to view its tabs.")
                } else if !state.tabs.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Picker("Tab", selection: $state.tabID) {
                            ForEach(state.tabs) { tab in
                                Text(tab.host.isEmpty ? tab.title : "\(tab.title) · \(tab.host)")
                                    .tag(tab.id)
                            }
                        }
                        .onChange(of: state.tabID) { _, _ in
                            state.screenshot = nil
                            state.previewIssue = nil
                        }
                        Button("Take over privately") {
                            Task { await send("takeover", body: ["tab": state.tabID]) }
                        }
                        .disabled(state.sending || state.tabID.isEmpty)
                        if let screenshot = state.screenshot {
                            Image(nsImage: screenshot)
                                .resizable().scaledToFit()
                                .frame(maxWidth: .infinity)
                                .accessibilityLabel("Preview of the selected browser tab")
                        } else if let previewIssue = state.previewIssue {
                            Text(previewIssue)
                        } else {
                            ProgressView().controlSize(.small)
                        }
                    }
                } else if state.enabled == true {
                    Text("No connected Browse tabs yet.")
                } else if state.problem == nil {
                    ProgressView().controlSize(.small)
                }
            }
            .foregroundStyle(Paper.inkSecondary)
            .padding(14)
            if let problem = state.problem {
                Text(problem).foregroundStyle(Paper.errorInk).padding(.horizontal, 14)
            }
            Spacer()
        }
        .foregroundStyle(Paper.inkPrimary)
        .background(Paper.canvas)
        .inspectorColumnWidth(min: state.expanded ? 470 : 265, ideal: state.expanded ? 650 : 340, max: 900)
        .task {
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(for: .seconds(state.privateInput ? 1 : 3))
            }
        }
    }

    private func refresh() async {
        guard !state.sending else { return }
        do {
            let status = try await selection.connection.send("GET", "browser/view")
            guard !state.sending, !Task.isCancelled else { return }
            state.privateInput = status["private"] as? Bool == true
            state.handoffOpen = status["handoff_open"] as? Bool == true
            if state.privateInput {
                state.screenshot = nil
                state.tabs = []
                state.tabID = ""
                return
            }
            state.problem = nil
            state.enabled = status["enabled"] as? Bool
            state.tabs = (status["tabs"] as? [[String: Any]] ?? []).compactMap(BrowserTab.init)
            if !state.tabs.contains(where: { $0.id == state.tabID }) {
                state.tabID = state.tabs.first?.id ?? ""
                state.screenshot = nil
            }
        } catch {
            state.screenshot = nil
            state.tabs = []
            state.problem = "\(error)"
            return
        }
        let target = state.tabID
        guard !target.isEmpty else { return }
        do {
            let data = try await selection.connection.getBytes(
                "browser/view/\(target)/image", queryItems: [])
            if !state.sending, !state.privateInput, target == state.tabID, !Task.isCancelled {
                state.screenshot = NSImage(data: data)
                state.previewIssue = state.screenshot == nil ? "Preview unavailable." : nil
            }
        } catch {
            if !state.sending, !state.privateInput, target == state.tabID, !Task.isCancelled {
                state.screenshot = nil
                state.previewIssue = "Preview unavailable."
            }
        }
    }

    private func send(_ action: String, body: [String: String]? = nil) async {
        guard !state.sending else { return }
        state.sending = true
        state.screenshot = nil
        state.previewIssue = nil
        state.problem = nil
        if action == "takeover" { state.privateInput = true }
        do {
            let stateResponse = try await selection.connection.send("POST", "browser/view/\(action)", json: body)
            state.privateInput = stateResponse["private"] as? Bool == true
            state.handoffOpen = state.privateInput
        } catch { state.problem = "\(error)" }
        state.sending = false
        await refresh()
    }
}
