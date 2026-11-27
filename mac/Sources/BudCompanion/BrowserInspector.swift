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

struct BrowserPreview {
    let image: NSImage
    let capturedAt: Date

    func freshness(at now: Date) -> String {
        let seconds = Int(now.timeIntervalSince(capturedAt))
        return switch seconds {
        case ..<1: "Just now"
        case ..<60: "Updated \(seconds)s ago"
        case ..<3_600: "Updated \(seconds / 60)m ago"
        case ..<86_400: "Updated \(seconds / 3_600)h ago"
        default: "Updated \(seconds / 86_400)d ago"
        }
    }
}

enum BrowserEmptyState: Equatable {
    case disconnected, disabled, noTabs
}

@MainActor
final class BrowserInspectorState: ObservableObject {
    @Published var groups: [BrowserTabGroup] = []
    @Published var focus = BrowserTabFocus()
    @Published var activeTabID: String?
    @Published private(set) var previews: [String: BrowserPreview] = [:]
    @Published private(set) var previewIssues: [String: String] = [:]
    @Published var expanded = false
    @Published var enabled: Bool?
    @Published var connected: Bool?
    @Published private(set) var privateInput = false
    @Published var handoffOpen = false
    @Published private(set) var controlledTabTitle: String?
    @Published var sending = false
    @Published var problem: String?
    @Published private(set) var refreshID = 0

    var visibleTabIDs: Set<String> = []
    private var lastRefreshedTabID: String?

    var tabCount: Int { groups.reduce(0) { $0 + $1.tabs.count } }
    var emptyState: BrowserEmptyState? {
        if privateInput { return nil }
        if connected == false { return .disconnected }
        if enabled == false { return .disabled }
        if connected == true, groups.isEmpty { return .noTabs }
        return nil
    }

    func requestRefresh() {
        refreshID += 1
    }

    func select(_ tabID: String) {
        guard tabID != focus.selected else { return }
        focus.selected = tabID
        if previews[tabID] == nil {
            requestRefresh()
        }
    }

    func applyStatus(_ status: [String: Any]) {
        if status["private"] as? Bool == true {
            enterPrivateInput(handoffOpen: status["handoff_open"] as? Bool == true)
            return
        }
        privateInput = false
        handoffOpen = false
        controlledTabTitle = nil
        problem = nil
        enabled = status["enabled"] as? Bool
        connected = status["connected"] as? Bool
        guard enabled != false, connected != false else {
            clearTabs()
            return
        }
        let tabs = (status["tabs"] as? [[String: Any]] ?? []).compactMap(BrowserTab.init)
        groups = BrowserTabGroup.make(
            tabs: tabs, browsers: status["browsers"] as? [[String: Any]] ?? [])
        activeTabID = status["active_tab"] as? String
        let tabIDs = groups.flatMap { $0.tabs.map(\.id) }
        focus.refresh(tabIDs: tabIDs, active: activeTabID)
        let currentIDs = Set(tabIDs)
        previews = previews.filter { currentIDs.contains($0.key) }
        previewIssues = previewIssues.filter { currentIDs.contains($0.key) }
    }

    func refresh(using connection: BudConnection, id: Int) async {
        guard await refreshStatus(using: connection, id: id) else { return }
        let selected = focus.selected
        guard !selected.isEmpty else { return }
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

    private func refreshStatus(using connection: BudConnection, id: Int) async -> Bool {
        guard canRefresh(id) else { return false }
        do {
            let status = try await connection.send("GET", "browser/view")
            guard canRefresh(id) else { return false }
            applyStatus(status)
            return !privateInput && emptyState == nil
        } catch {
            guard canRefresh(id) else { return false }
            if case ConnectionError.http(404, _) = error {
                applyStatus(["enabled": false, "connected": false])
            } else {
                connectionFailed(error)
            }
            return false
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
            case ConnectionError.http(401, _), ConnectionError.http(403, _), ConnectionError.http(409, _):
                connectionFailed(error)
                return
            case ConnectionError.unreachable, ConnectionError.http(404, _), ConnectionError.http(502, _):
                // Check whether the tab and Browse are still available after an ambiguous capture failure.
                guard await refreshStatus(using: connection, id: id),
                      canRefresh(id),
                      groups.contains(where: { $0.tabs.contains { $0.id == tabID } }) else { return }
                image = nil
            default:
                image = nil
            }
        }
        recordPreview(image, for: tabID)
    }

    func recordPreview(_ image: NSImage?, for tabID: String, at capturedAt: Date = .now) {
        if let image {
            previews[tabID] = BrowserPreview(image: image, capturedAt: capturedAt)
            previewIssues[tabID] = nil
        } else {
            previewIssues[tabID] = previews[tabID] == nil ? "Preview unavailable." : "Refresh failed."
        }
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
        focus = BrowserTabFocus()
        clearPreviews()
    }

    private func clearPreviews() {
        previews = [:]
        previewIssues = [:]
    }

    func send(_ action: String, using connection: BudConnection, body: [String: String]? = nil) async {
        guard !sending else { return }
        if action == "takeover", let tabID = body?["tab"],
           let tab = groups.flatMap(\.tabs).first(where: { $0.id == tabID }) {
            let trimmed = tab.title.trimmingCharacters(in: .whitespacesAndNewlines)
            controlledTabTitle = trimmed.isEmpty ? (tab.host.isEmpty ? nil : tab.host) : trimmed
        }
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
                controlledTabTitle = nil
            }
        } catch ConnectionError.http(404, _) {
            applyStatus(["enabled": false, "connected": false])
        } catch {
            problem = "\(error)"
        }
        sending = false
        requestRefresh()
    }
}

@MainActor
struct BrowserInspector: View {
    let selection: BrowserSelection
    let close: () -> Void
    @StateObject private var state = BrowserInspectorState()
    @Environment(\.openSettings) private var openSettings

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
            } else if let emptyState = state.emptyState {
                emptyContent(emptyState)
            } else if !state.groups.isEmpty {
                tabList
            } else if let problem = state.problem {
                guidance(problem) { connectionActions }
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
            .help(state.expanded ? "Narrow browser" : "Expand browser")
            .accessibilityLabel(state.expanded ? "Narrow browser" : "Expand browser")
            Button(action: close) { Image(systemName: "xmark") }
                .buttonStyle(IconButton(size: 26))
                .help("Close browser")
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
        let preview = state.previews[tab.id]
        let issue = state.previewIssues[tab.id]
        let shape = RoundedRectangle(cornerRadius: Paper.radiusMD, style: .continuous)
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                state.select(tab.id)
            } label: {
                Paper.recessed
                    .aspectRatio(16.0 / 10.0, contentMode: .fit)
                    .overlay {
                        if let preview {
                            Image(nsImage: preview.image)
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
                    .overlay { PaperOutline(shape: shape, focused: selected) }
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
                        Button(action: state.requestRefresh) { Image(systemName: "arrow.clockwise") }
                            .font(.system(size: 12, weight: .medium))
                            .buttonStyle(IconButton())
                            .help("Refresh preview")
                            .accessibilityLabel("Refresh preview")
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
                        Text("Bud's tab")
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
                if let preview {
                    if selected {
                        TimelineView(.periodic(from: preview.capturedAt, by: 1)) { context in
                            Text(preview.freshness(at: context.date))
                        }
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkMuted)
                    }
                    if let issue {
                        Text(issue)
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkSecondary)
                    }
                }
            }
            .padding(.top, 6)
        }
    }

    private var privateInput: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label {
                Text("You have control").font(Paper.Typography.label)
            } icon: {
                Image(systemName: "hand.raised.fill").font(.system(size: 13))
            }
            .foregroundStyle(Paper.inkPrimary)
            if let controlledTabTitle = state.controlledTabTitle {
                Text(controlledTabTitle)
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkPrimary)
                    .lineLimit(2)
            }
            Text(state.handoffOpen
                 ? "Bud is paused on this tab while you work in the browser. Select Continue in the tab when you're done."
                 : "Bud is paused on this tab while you work in the browser. Give back control when you're done.")
                .font(Paper.Typography.callout)
                .foregroundStyle(Paper.inkSecondary)
            if !state.handoffOpen, !state.sending {
                Button("Give back control") { Task { await state.send("resume", using: selection.connection) } }
                    .buttonStyle(CardButton(kind: .primary))
            }
        }
    }

    @ViewBuilder
    private func emptyContent(_ emptyState: BrowserEmptyState) -> some View {
        switch emptyState {
        case .disconnected:
            guidance("Browser not connected.") { connectionActions }
        case .disabled:
            guidance("Connect and enable Browse from chat to view its tabs.") { connectionActions }
        case .noTabs:
            guidance("No tabs yet. Open a tab on this Mac or a paired computer, then lend it from the Browse extension.") {
                Button("New tab", action: openNewTab)
                    .buttonStyle(CardButton(kind: .secondary))
                    .help("Open a new Chrome tab on this Mac")
            }
        }
    }

    private var connectionActions: some View {
        HStack(spacing: 8) {
            Button("Check connection", action: state.requestRefresh)
                .buttonStyle(CardButton(kind: .secondary))
            Button("Browse setup", action: openBrowseSetup)
                .buttonStyle(CardButton(kind: .quiet))
        }
    }

    private func openBrowseSetup() {
        SettingsTabState.shared.selected = .computers
        openSettings()
        Task { await SettingsTabState.shared.bringSettingsToFront() }
    }

    private func openNewTab() {
        let workspace = NSWorkspace.shared
        guard let chrome = workspace.urlForApplication(withBundleIdentifier: "com.google.Chrome") else {
            openBrowseSetup()
            return
        }
        Task {
            do {
                _ = try await workspace.open([URL(string: "about:blank")!], withApplicationAt: chrome,
                                             configuration: NSWorkspace.OpenConfiguration())
                state.requestRefresh()
            } catch {
                openBrowseSetup()
            }
        }
    }

    private func guidance<Actions: View>(
        _ message: String, @ViewBuilder actions: () -> Actions
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label {
                Text(message).font(Paper.Typography.callout)
            } icon: {
                Image(systemName: "macwindow").font(.system(size: 14))
            }
            .foregroundStyle(Paper.inkSecondary)
            actions()
        }
        .padding(14)
    }
}
