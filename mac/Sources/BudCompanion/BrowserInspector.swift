import AppKit
import SwiftUI

struct BrowserSelection: Identifiable {
    let connection: BudConnection
    let id = UUID()
}

struct BrowserTab: Identifiable {
    let id: String
    let title: String
    let host: String
    let browser: String?

    init?(_ raw: [String: Any]) {
        guard let id = raw["id"] as? String, let title = raw["title"] as? String else {
            return nil
        }
        self.id = id
        self.title = title
        self.host = raw["host"] as? String ?? ""
        browser = raw["browser"] as? String
    }
}

struct BrowserTabGroup: Identifiable {
    let id: String?
    let name: String
    let tabs: [BrowserTab]

    static func make(tabs: [BrowserTab], browsers: [[String: Any]]) -> [Self] {
        var byBrowser = Dictionary(grouping: tabs, by: \.browser)
        let named = browsers.compactMap { source -> Self? in
            guard let id = source["id"] as? String, let name = source["name"] as? String,
                  let tabs = byBrowser.removeValue(forKey: id) else { return nil }
            return Self(id: id, name: name, tabs: tabs)
        }
        let otherTabs = tabs.filter { byBrowser[$0.browser] != nil }
        return otherTabs.isEmpty ? named : named + [Self(id: nil, name: "Browse", tabs: otherTabs)]
    }
}

struct BrowserTabFocus {
    var selected = ""
    private var lastActive: String?

    mutating func refresh(tabIDs: [String], active: String?) {
        if let active, active != lastActive, tabIDs.contains(active) {
            selected = active
        } else if !tabIDs.contains(selected) {
            selected = active.flatMap { tabIDs.contains($0) ? $0 : nil } ?? tabIDs.first ?? ""
        }
        lastActive = active
    }
}

@MainActor
private final class BrowserInspectorState: ObservableObject {
    @Published var groups: [BrowserTabGroup] = []
    @Published var focus = BrowserTabFocus()
    @Published var screenshot: NSImage?
    @Published var expanded = false
    @Published var enabled: Bool?
    @Published var connected: Bool?
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
                Text("Browser").font(Paper.Typography.callout)
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
                            Text("Finish in the browser, then select Continue in the tab.")
                        } else if !state.sending {
                            Text("Finish private input in the browser before resuming.")
                            Button("Resume Bud") { Task { await send("resume") } }
                                .buttonStyle(CardButton(kind: .primary))
                        }
                    }
                } else if state.enabled == false {
                    Text("Connect and enable Browse from chat to view its tabs.")
                } else if state.connected == false {
                    Text("No browser is connected. Open a browser with the Browse extension enabled.")
                } else if !state.groups.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Picker("Tab", selection: Binding(
                            get: { state.focus.selected },
                            set: {
                                state.focus.selected = $0
                                clearPreview()
                            }
                        )) {
                            ForEach(state.groups) { group in
                                Section(group.name) {
                                    ForEach(group.tabs) { tab in
                                        Text(tab.host.isEmpty ? tab.title : "\(tab.title) · \(tab.host)")
                                            .tag(tab.id)
                                    }
                                }
                            }
                        }
                        Button("Take over privately") {
                            let tab = state.focus.selected
                            Task { await send("takeover", body: ["tab": tab]) }
                        }
                        .buttonStyle(CardButton(kind: .secondary))
                        .disabled(state.sending || state.focus.selected.isEmpty)
                        .opacity(state.sending ? 0.55 : 1)
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
                    Text("No tabs to show. Ask Bud to open a tab, or lend one from the Browse extension.")
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
                clearPreview()
                state.groups = []
                return
            }
            state.problem = nil
            state.enabled = status["enabled"] as? Bool
            state.connected = status["connected"] as? Bool
            let tabs = (status["tabs"] as? [[String: Any]] ?? []).compactMap(BrowserTab.init)
            state.groups = BrowserTabGroup.make(
                tabs: tabs, browsers: status["browsers"] as? [[String: Any]] ?? [])
            let previous = state.focus.selected
            state.focus.refresh(tabIDs: state.groups.flatMap { $0.tabs.map(\.id) },
                                active: status["active_tab"] as? String)
            if state.focus.selected != previous { clearPreview() }
        } catch {
            guard !state.sending, !Task.isCancelled else { return }
            state.screenshot = nil
            state.groups = []
            state.enabled = nil
            state.connected = nil
            if case ConnectionError.http(502, let detail) = error {
                state.problem = detail
            } else {
                state.problem = "\(error)"
            }
            return
        }
        let target = state.focus.selected
        guard !target.isEmpty else { return }
        do {
            let data = try await selection.connection.getBytes(
                "browser/view/\(target)/image", queryItems: [])
            if !state.sending, !state.privateInput, target == state.focus.selected, !Task.isCancelled {
                state.screenshot = NSImage(data: data)
                state.previewIssue = state.screenshot == nil ? "Preview unavailable." : nil
            }
        } catch {
            if !state.sending, !state.privateInput, target == state.focus.selected, !Task.isCancelled {
                state.screenshot = nil
                state.previewIssue = "Preview unavailable."
            }
        }
    }

    private func clearPreview() {
        state.screenshot = nil
        state.previewIssue = nil
    }

    private func send(_ action: String, body: [String: String]? = nil) async {
        guard !state.sending else { return }
        state.sending = true
        clearPreview()
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
