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
final class BrowserInspectorState: ObservableObject {
    @Published var groups: [BrowserTabGroup] = []
    @Published var focus = BrowserTabFocus()
    @Published var activeTabID: String?
    @Published var screenshots: [String: NSImage] = [:]
    @Published var previewIssues: [String: String] = [:]
    @Published var expanded = false
    @Published var enabled: Bool?
    @Published var connected: Bool?
    @Published private(set) var privateInput = false
    @Published var handoffOpen = false
    @Published var sending = false
    @Published var problem: String?
    @Published private(set) var refreshID = 0

    var visibleTabIDs: Set<String> = []
    private var lastRefreshedTabID: String?

    var tabCount: Int { groups.reduce(0) { $0 + $1.tabs.count } }
    var previewIssue: String? { previewIssues[focus.selected] }

    func select(_ tabID: String) {
        guard tabID != focus.selected else { return }
        focus.selected = tabID
        if screenshots[tabID] == nil {
            refreshID += 1
        }
    }

    func applyStatus(_ status: [String: Any]) {
        if status["private"] as? Bool == true {
            enterPrivateInput(handoffOpen: status["handoff_open"] as? Bool == true)
            return
        }
        privateInput = false
        handoffOpen = false
        problem = nil
        enabled = status["enabled"] as? Bool
        connected = status["connected"] as? Bool
        let tabs = (status["tabs"] as? [[String: Any]] ?? []).compactMap(BrowserTab.init)
        groups = BrowserTabGroup.make(
            tabs: tabs, browsers: status["browsers"] as? [[String: Any]] ?? [])
        activeTabID = status["active_tab"] as? String
        let tabIDs = groups.flatMap { $0.tabs.map(\.id) }
        focus.refresh(tabIDs: tabIDs, active: activeTabID)
        let currentIDs = Set(tabIDs)
        screenshots = screenshots.filter { currentIDs.contains($0.key) }
        previewIssues = previewIssues.filter { currentIDs.contains($0.key) }
    }

    func refresh(using connection: BudConnection, id: Int) async {
        guard canRefresh(id) else { return }
        do {
            let status = try await connection.send("GET", "browser/view")
            guard canRefresh(id) else { return }
            applyStatus(status)
        } catch {
            guard canRefresh(id) else { return }
            if case ConnectionError.http(404, _) = error {
                applyStatus(["enabled": false])
            } else {
                connectionFailed(error)
            }
            return
        }

        let selected = focus.selected
        guard !privateInput, !selected.isEmpty else { return }
        await refreshPreview(selected, using: connection, id: id)

        let otherTabs = groups.flatMap(\.tabs).map(\.id).filter {
            $0 != selected && visibleTabIDs.contains($0)
        }
        let start = otherTabs.firstIndex { $0 == lastRefreshedTabID }.map { $0 + 1 } ?? 0
        for tabID in (otherTabs[start...] + otherTabs[..<start]).prefix(4) {
            guard canRefresh(id), !privateInput, !groups.isEmpty else { break }
            guard visibleTabIDs.contains(tabID) else { continue }
            lastRefreshedTabID = tabID
            await refreshPreview(tabID, using: connection, id: id)
        }
    }

    private func refreshPreview(_ tabID: String, using connection: BudConnection, id: Int) async {
        let image: NSImage?
        do {
            let data = try await connection.getBytes("browser/view/\(tabID)/image", queryItems: [])
            guard canRefresh(id), !privateInput else { return }
            image = NSImage(data: data)
        } catch {
            guard canRefresh(id), !privateInput else { return }
            switch error {
            case ConnectionError.http(423, _):
                enterPrivateInput(handoffOpen: true)
                return
            case ConnectionError.unreachable,
                 ConnectionError.http(401, _), ConnectionError.http(403, _), ConnectionError.http(409, _):
                connectionFailed(error)
                return
            default:
                image = nil
            }
        }
        screenshots[tabID] = image
        previewIssues[tabID] = image == nil ? "Preview unavailable." : nil
    }

    private func canRefresh(_ id: Int) -> Bool {
        id == refreshID && !sending && !Task.isCancelled
    }

    private func enterPrivateInput(handoffOpen: Bool) {
        privateInput = true
        self.handoffOpen = handoffOpen
        clearTabs()
    }

    private func connectionFailed(_ error: Error) {
        clearTabs()
        enabled = nil
        connected = nil
        if case ConnectionError.http(502, let detail) = error {
            problem = detail
        } else {
            problem = "\(error)"
        }
    }

    private func clearTabs() {
        activeTabID = nil
        groups = []
        clearPreviews()
    }

    private func clearPreviews() {
        screenshots = [:]
        previewIssues = [:]
    }

    func send(_ action: String, using connection: BudConnection, body: [String: String]? = nil) async {
        guard !sending else { return }
        sending = true
        clearPreviews()
        problem = nil
        do {
            let response = try await connection.send("POST", "browser/view/\(action)", json: body)
            if response["private"] as? Bool == true {
                enterPrivateInput(handoffOpen: true)
            } else {
                privateInput = false
                handoffOpen = false
            }
        } catch ConnectionError.http(404, _) {
            applyStatus(["enabled": false])
        } catch {
            problem = "\(error)"
        }
        sending = false
        refreshID += 1
    }
}

@MainActor
struct BrowserInspector: View {
    let selection: BrowserSelection
    let close: () -> Void
    @StateObject private var state = BrowserInspectorState()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            if state.privateInput {
                privateInput
                    .padding(14)
            } else if state.sending {
                VStack(alignment: .leading, spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text("Wait for Private input before entering private information.")
                }
                .font(Paper.Typography.callout)
                .foregroundStyle(Paper.inkSecondary)
                .padding(14)
            } else if state.enabled == false {
                guidance("Connect and enable Browse from chat to view its tabs.")
            } else if state.connected == false {
                guidance("Open a browser with the Browse extension enabled to connect.")
            } else if !state.groups.isEmpty {
                tabList
            } else if state.enabled == true {
                guidance("Ask Bud to open a tab, or lend one from the Browse extension.")
            } else if let problem = state.problem {
                guidance(problem)
            } else {
                ProgressView().controlSize(.small).padding(14)
            }
            if state.privateInput, let problem = state.problem {
                Text(problem)
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.errorInk)
                    .padding(.horizontal, 14)
            }
        }
        .frame(maxHeight: .infinity, alignment: .topLeading)
        .background(Paper.canvas)
        .inspectorColumnWidth(min: state.expanded ? 470 : 265, ideal: state.expanded ? 650 : 340, max: 900)
        .task(id: state.refreshID) { [id = state.refreshID] in
            while !Task.isCancelled, id == state.refreshID {
                await state.refresh(using: selection.connection, id: id)
                try? await Task.sleep(for: .seconds(state.privateInput ? 1 : 3))
            }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Browser")
                    .font(Paper.Typography.label)
                    .foregroundStyle(Paper.inkPrimary)
                let count = state.tabCount
                if count > 0, !state.privateInput, !state.sending {
                    Text("\(count) \(count == 1 ? "tab" : "tabs")")
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkMuted)
                }
            }
            Spacer()
            Button { state.expanded.toggle() } label: {
                Image(systemName: state.expanded
                    ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
            }
            .buttonStyle(IconButton(size: 26))
            .accessibilityLabel(state.expanded ? "Narrow browser" : "Expand browser")
            Button(action: close) { Image(systemName: "xmark") }
                .buttonStyle(IconButton(size: 26))
                .accessibilityLabel("Close browser")
        }
        .font(.system(size: 12, weight: .medium))
        .padding(.horizontal, 14)
        .frame(height: 40)
    }

    private var tabList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) {
                ForEach(state.groups) { group in
                    if state.groups.count > 1 {
                        if group.id != state.groups.first?.id {
                            Rectangle().fill(Paper.hairline).frame(height: 0.5)
                        }
                        Text(group.name)
                            .font(Paper.Typography.captionMedium)
                            .foregroundStyle(Paper.inkMuted)
                    }
                    ForEach(group.tabs) { tab in
                        tabCard(tab)
                            .onScrollVisibilityChange(threshold: 0.01) { visible in
                                if visible {
                                    state.visibleTabIDs.insert(tab.id)
                                } else {
                                    state.visibleTabIDs.remove(tab.id)
                                }
                            }
                    }
                }
            }
            .padding(14)
        }
    }

    private func tabCard(_ tab: BrowserTab) -> some View {
        let selected = tab.id == state.focus.selected
        let fallbackTitle = tab.host.isEmpty ? "Untitled tab" : tab.host
        let title = tab.title.isEmpty ? fallbackTitle : tab.title
        let previewLabel = selected ? "Preview of the selected browser tab" : "Preview of \(title)"
        let issue = selected ? state.previewIssue : state.previewIssues[tab.id]
        let shape = RoundedRectangle(cornerRadius: Paper.radiusMD, style: .continuous)
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                state.select(tab.id)
            } label: {
                Paper.recessed
                    .aspectRatio(16.0 / 10.0, contentMode: .fit)
                    .overlay {
                        if let image = state.screenshots[tab.id] {
                            Image(nsImage: image)
                                .resizable()
                                .scaledToFit()
                                .accessibilityLabel(previewLabel)
                        } else if let issue {
                            VStack(spacing: 6) {
                                Image(systemName: "macwindow")
                                Text(issue).font(Paper.Typography.caption)
                            }
                            .foregroundStyle(Paper.inkMuted)
                        } else {
                            ProgressView().controlSize(.small)
                        }
                    }
                    .clipShape(shape)
                    .overlay {
                        shape.strokeBorder(selected ? Paper.accent : Paper.borderSubtle,
                                           lineWidth: selected ? 1.5 : 0.5)
                    }
                    .contentShape(shape)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(previewLabel)
            .accessibilityValue(issue ?? "")
            .accessibilityAddTraits(selected ? .isSelected : [])

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(title)
                        .font(Paper.Typography.label)
                        .foregroundStyle(Paper.inkPrimary)
                        .lineLimit(1)
                    Spacer(minLength: 8)
                    if selected {
                        Button("Take over") {
                            Task {
                                await state.send("takeover", using: selection.connection, body: ["tab": tab.id])
                            }
                        }
                        .buttonStyle(CardButton(kind: .secondary))
                        .help("Take over privately")
                        .accessibilityLabel("Take over privately")
                        .disabled(state.sending)
                    }
                }
                HStack(spacing: 6) {
                    if tab.id == state.activeTabID {
                        Circle()
                            .fill(Paper.approveInk)
                            .frame(width: 6, height: 6)
                            .accessibilityHidden(true)
                        Text("Live")
                            .font(Paper.Typography.captionMedium)
                            .foregroundStyle(Paper.inkSecondary)
                        if !tab.host.isEmpty { Text("·").accessibilityHidden(true) }
                    }
                    if !tab.host.isEmpty {
                        Text(tab.host).lineLimit(1)
                    }
                }
                .font(Paper.Typography.caption)
                .foregroundStyle(Paper.inkMuted)
            }
            .padding(.top, 6)
        }
    }

    private var privateInput: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label {
                Text("Private input").font(Paper.Typography.label)
            } icon: {
                Image(systemName: "lock.fill").font(.system(size: 14))
            }
            .foregroundStyle(Paper.inkPrimary)
            Text(state.handoffOpen
                 ? "Finish in the browser, then select Continue in the tab."
                 : "Finish private input in the browser before resuming Bud.")
                .font(Paper.Typography.callout)
                .foregroundStyle(Paper.inkSecondary)
            if !state.handoffOpen, !state.sending {
                Button("Resume Bud") { Task { await state.send("resume", using: selection.connection) } }
                    .buttonStyle(CardButton(kind: .primary))
            }
        }
    }

    private func guidance(_ message: String) -> some View {
        Label {
            Text(message).font(Paper.Typography.callout)
        } icon: {
            Image(systemName: "macwindow").font(.system(size: 14))
        }
        .foregroundStyle(Paper.inkSecondary)
        .padding(14)
    }
}
