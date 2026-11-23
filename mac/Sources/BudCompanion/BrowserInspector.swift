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
struct BrowserInspector: View {
    let selection: BrowserSelection
    let close: () -> Void
    @State private var tabs: [BrowserTab] = []
    @State private var tabID = ""
    @State private var screenshot: NSImage?
    @State private var expanded = false
    @State private var enabled: Bool?
    @State private var privateInput = false
    @State private var handoffOpen = false
    @State private var sending = false
    @State private var problem: String?
    @State private var previewIssue: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Browser").font(Paper.Typography.callout)
                    Text(selection.local ? "Your Chrome on this Mac" : "Chrome on the Bud server")
                        .font(Paper.Typography.ledgerMeta).foregroundStyle(Paper.inkSecondary)
                }
                Spacer()
                Button { expanded.toggle() } label: {
                    Image(systemName: expanded
                        ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                }
                .buttonStyle(IconButton(size: 24))
                .accessibilityLabel(expanded ? "Narrow browser" : "Expand browser")
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(IconButton(size: 24))
                    .accessibilityLabel("Close browser")
            }
            .padding(12)
            Divider()
            Group {
                if privateInput {
                    VStack(alignment: .leading, spacing: 12) {
                        Label("Private input", systemImage: "lock.fill")
                            .font(Paper.Typography.callout)
                        Text("Bud’s browser actions and preview are paused.")
                        if handoffOpen {
                            Text("Finish in Chrome, then select Continue in the tab.")
                        } else if !sending {
                            Text("Finish private input in Chrome before resuming.")
                            Button("Resume Bud") { Task { await send("resume") } }
                        }
                    }
                } else if enabled == false {
                    Text("Connect and enable Browse from chat to view its tabs.")
                } else if !tabs.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Picker("Tab", selection: $tabID) {
                            ForEach(tabs) { tab in
                                Text(tab.host.isEmpty ? tab.title : "\(tab.title) · \(tab.host)")
                                    .tag(tab.id)
                            }
                        }
                        .onChange(of: tabID) { _, _ in
                            screenshot = nil
                            previewIssue = nil
                        }
                        Button("Take over privately") {
                            Task { await send("takeover", body: ["tab": tabID]) }
                        }
                        .disabled(sending || tabID.isEmpty)
                        if let screenshot {
                            Image(nsImage: screenshot)
                                .resizable().scaledToFit()
                                .frame(maxWidth: .infinity)
                                .accessibilityLabel("Preview of the selected browser tab")
                        } else if let previewIssue {
                            Text(previewIssue)
                        } else {
                            ProgressView().controlSize(.small)
                        }
                    }
                } else if enabled == true {
                    Text("No connected Browse tabs yet.")
                } else if problem == nil {
                    ProgressView().controlSize(.small)
                }
            }
            .foregroundStyle(Paper.inkSecondary)
            .padding(14)
            if let problem {
                Text(problem).foregroundStyle(Paper.errorInk).padding(.horizontal, 14)
            }
            Spacer()
        }
        .foregroundStyle(Paper.inkPrimary)
        .background(Paper.canvas)
        .inspectorColumnWidth(min: expanded ? 470 : 265, ideal: expanded ? 650 : 340, max: 900)
        .task {
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(for: .seconds(privateInput ? 1 : 3))
            }
        }
    }

    private func refresh() async {
        guard !sending else { return }
        do {
            let status = try await selection.connection.send("GET", "browser/view")
            guard !sending, !Task.isCancelled else { return }
            privateInput = status["private"] as? Bool == true
            handoffOpen = status["handoff_open"] as? Bool == true
            if privateInput {
                screenshot = nil
                tabs = []
                tabID = ""
                return
            }
            problem = nil
            enabled = status["enabled"] as? Bool
            tabs = (status["tabs"] as? [[String: Any]] ?? []).compactMap(BrowserTab.init)
            if !tabs.contains(where: { $0.id == tabID }) {
                tabID = tabs.first?.id ?? ""
                screenshot = nil
            }
        } catch {
            screenshot = nil
            tabs = []
            problem = "\(error)"
            return
        }
        let target = tabID
        guard !target.isEmpty else { return }
        do {
            let data = try await selection.connection.getBytes(
                "browser/view/\(target)/image", queryItems: [])
            if !sending, !privateInput, target == tabID, !Task.isCancelled {
                screenshot = NSImage(data: data)
                previewIssue = screenshot == nil ? "Preview unavailable." : nil
            }
        } catch {
            if !sending, !privateInput, target == tabID, !Task.isCancelled {
                screenshot = nil
                previewIssue = "Preview unavailable."
            }
        }
    }

    private func send(_ action: String, body: [String: Any]? = nil) async {
        guard !sending else { return }
        sending = true
        screenshot = nil
        previewIssue = nil
        problem = nil
        if action == "takeover" { privateInput = true }
        do {
            let state = try await selection.connection.send("POST", "browser/view/\(action)", json: body)
            privateInput = state["private"] as? Bool == true
            handoffOpen = privateInput
        } catch { problem = "\(error)" }
        sending = false
        await refresh()
    }
}
