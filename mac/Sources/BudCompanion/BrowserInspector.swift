import AppKit
import SwiftUI

struct BrowserSelection: Identifiable {
    let connection: BudConnection
    let id = UUID()
}

struct BrowserFrameSelection: Identifiable {
    let frame: TurnProvenance.BrowserFrame
    let connection: BudConnection
    var id: String { frame.id }
}

enum ChromePause: Equatable {
    case handoff, manual

    init?(_ status: [String: Any]) {
        guard status["private"] as? Bool == true, status["interactive"] as? Bool != true else { return nil }
        self = status["handoff_open"] as? Bool == true ? .handoff : .manual
    }

    var message: String {
        switch self {
        case .handoff: "Chrome needs your input. Finish there, then select Continue."
        case .manual: "Finish in Chrome, then give control back to Bud."
        }
    }
}

struct ChromePrivateInput: View {
    let pause: ChromePause
    let busy: Bool
    let onOpenFailure: () -> Void
    let onReturn: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(pause.message)
                .font(Paper.Typography.callout)
                .foregroundStyle(Paper.inkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Open Chrome") {
                    Task { if !(await activateChrome()) { onOpenFailure() } }
                }
                .buttonStyle(CardButton(kind: .quiet))
                if pause == .manual {
                    Button("Give back control", action: onReturn)
                        .buttonStyle(CardButton(kind: .primary))
                        .disabled(busy)
                }
            }
        }
    }
}

struct SavedBrowserFrameInspector: View {
    let selection: BrowserFrameSelection
    let companion: Companion
    let close: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Saved browser frame").font(Paper.Typography.label)
                Spacer()
                Button(action: close) { Image(systemName: "xmark").font(.system(size: 12, weight: .medium)) }
                    .buttonStyle(IconButton(size: 26))
                    .accessibilityLabel("Close saved frame")
            }
            .padding(.horizontal, 14)
            .frame(height: 40)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            if companion.browserPrivate {
                Text("Frames are hidden during private input.")
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
                    .padding(14)
            } else {
                SavedFramePreview(selection: selection)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Paper.canvas)
        .inspectorColumnWidth(min: 265, ideal: 380, max: 750)
    }
}

private struct SavedFramePreview: View {
    let selection: BrowserFrameSelection
    @StateObject private var state = PreviewState()

    private final class PreviewState: ObservableObject {
        @Published var image: NSImage?
        @Published var message: String?
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            let frame = selection.frame
            Text(frame.title.isEmpty ? frame.domain : frame.title)
                .font(Paper.Typography.callout)
                .lineLimit(2)
            if let image = state.image {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFit()
                    .accessibilityLabel("Captured page from \(frame.domain)")
            } else if let message = state.message {
                Text(message)
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
            } else {
                ProgressView().controlSize(.small).accessibilityLabel("Loading frame")
            }
        }
        .padding(14)
        .task {
            let connection = selection.connection
            do {
                guard try await permitsPreview(using: connection) else { return }
                let bytes: Data
                do {
                    bytes = try await connection.browserFrame(selection.id)
                } catch ConnectionError.http(404, _) {
                    if !Task.isCancelled { state.message = "Frame no longer saved" }
                    return
                }
                guard try await permitsPreview(using: connection) else { return }
                guard let image = NSImage(data: bytes) else {
                    state.message = "Could not load frame"
                    return
                }
                state.image = image
            } catch {
                if !Task.isCancelled { state.message = "Could not load frame" }
            }
        }
    }

    private func permitsPreview(using connection: BudConnection) async throws -> Bool {
        let status: [String: Any]
        do {
            status = try await connection.send("GET", "browser/view")
        } catch ConnectionError.http(404, _) {
            // Bud can still serve saved frames when live browser tools are disabled.
            return !Task.isCancelled
        }
        guard !Task.isCancelled else { return false }
        guard status["private"] as? Bool == false else {
            state.message = "Frames are hidden during private input."
            return false
        }
        return true
    }
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

    var displayTitle: String {
        let name = title.trimmingCharacters(in: .whitespacesAndNewlines)
        if !host.isEmpty, name.isEmpty || name.lowercased() == "about:blank" { return host }
        return name.isEmpty ? "Untitled tab" : name
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

enum BrowserControl: Equatable {
    case interactive(tabID: String, title: String)
    case chrome(title: String?, handoffOpen: Bool)
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
    @Published private(set) var control: BrowserControl?
    @Published var address = ""
    @Published var quickText = ""
    @Published var sending = false
    @Published var problem: String?
    @Published private(set) var refreshID = 0

    var visibleTabIDs: Set<String> = []
    private var lastRefreshedTabID: String?

    var tabCount: Int { groups.reduce(0) { $0 + $1.tabs.count } }
    var interactiveTabID: String? {
        if case .interactive(let tabID, _) = control { return tabID }
        return nil
    }
    var privateInput: Bool {
        if case .chrome = control { return true }
        return false
    }
    var handoffOpen: Bool {
        if case .chrome(_, let open) = control { return open }
        return false
    }
    var emptyState: BrowserEmptyState? {
        if control != nil { return nil }
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
        let tabs = (status["tabs"] as? [[String: Any]] ?? []).compactMap(BrowserTab.init)
        if status["interactive"] as? Bool == true,
           let tabID = status["takeover_tab"] as? String {
            let title = tabs.first { $0.id == tabID }?.displayTitle
            enterInteractive(tabID: tabID, title: title)
            return
        }
        if status["private"] as? Bool == true {
            enterPrivateInput(handoffOpen: status["handoff_open"] as? Bool == true)
            return
        }
        endControl()
        problem = nil
        enabled = status["enabled"] as? Bool
        connected = status["connected"] as? Bool
        guard enabled != false, connected != false else {
            clearTabs()
            return
        }
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
        if let tabID = interactiveTabID {
            await refreshPreview(tabID, using: connection, id: id)
            return
        }
        let selected = focus.selected
        guard !selected.isEmpty else { return }
        await refreshPreview(selected, using: connection, id: id)

        let otherTabs = groups.flatMap(\.tabs).map(\.id).filter {
            $0 != selected && visibleTabIDs.contains($0)
        }
        let start = otherTabs.firstIndex { $0 == lastRefreshedTabID }.map { $0 + 1 } ?? 0
        for tabID in (otherTabs[start...] + otherTabs[..<start]).prefix(4) {
            guard canRefresh(id), control == nil, !groups.isEmpty else { break }
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
            switch error {
            case ConnectionError.unreachable, ConnectionError.http(502, _):
                if let tabID = interactiveTabID {
                    recordPreview(nil, for: tabID)
                } else {
                    connectionFailed(error)
                }
            case ConnectionError.http(404, _):
                connectionFailed(error)
                connected = false
            default:
                connectionFailed(error)
            }
            return false
        }
    }

    private func refreshPreview(_ tabID: String, using connection: BudConnection, id: Int) async {
        let image: NSImage?
        do {
            let data = try await connection.getBytes("browser/view/\(tabID)/image", queryItems: [])
            guard canCapture(tabID, id: id) else { return }
            image = NSImage(data: data)
        } catch {
            guard canCapture(tabID, id: id) else { return }
            switch error {
            case ConnectionError.http(423, _):
                if interactiveTabID == nil {
                    enterPrivateInput(handoffOpen: true)
                    return
                }
                guard await refreshStatus(using: connection, id: id),
                      canCapture(tabID, id: id) else { return }
                image = nil
            case ConnectionError.http(401, _), ConnectionError.http(403, _), ConnectionError.http(409, _):
                connectionFailed(error)
                return
            case ConnectionError.unreachable, ConnectionError.http(404, _), ConnectionError.http(502, _):
                // Check whether the tab and Browse are still available after an ambiguous capture failure.
                guard await refreshStatus(using: connection, id: id),
                      canCapture(tabID, id: id),
                      interactiveTabID == tabID || groups.contains(where: {
                          $0.tabs.contains { $0.id == tabID }
                      }) else { return }
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

    private func canCapture(_ tabID: String, id: Int) -> Bool {
        canRefresh(id) && !privateInput && (interactiveTabID ?? tabID) == tabID
    }

    private func enterInteractive(tabID: String, title: String?) {
        let name: String
        if case .interactive(let previousID, let previousTitle) = control, previousID == tabID {
            name = title ?? previousTitle
        } else {
            name = title ?? groups.flatMap(\.tabs).first { $0.id == tabID }?.displayTitle ?? "Browser tab"
            previews = previews.filter { $0.key == tabID }
            previewIssues = previewIssues.filter { $0.key == tabID }
            address = ""
            quickText = ""
            focus.selected = tabID
        }
        let next = BrowserControl.interactive(tabID: tabID, title: name)
        if control != next { control = next }
    }

    private func enterPrivateInput(handoffOpen: Bool, title: String? = nil) {
        var name = title
        if name == nil, case .chrome(let previousTitle, _) = control { name = previousTitle }
        let next = BrowserControl.chrome(title: name, handoffOpen: handoffOpen)
        guard control != next else { return }
        endControl()
        control = next
        clearTabs()
    }

    private func endControl() {
        guard control != nil else { return }
        control = nil
        address = ""
        quickText = ""
        clearPreviews()
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

    func takeOver(_ tab: BrowserTab, using connection: BudConnection) async {
        guard control == nil else { return }
        await post("takeover", using: connection, body: ["tab": tab.id]) { response in
            if response["interactive"] as? Bool == true, let tabID = response["tab"] as? String {
                enterInteractive(tabID: tabID, title: tab.displayTitle)
            } else if response["private"] as? Bool == true {
                enterPrivateInput(handoffOpen: response["handoff_open"] as? Bool ?? true, title: tab.displayTitle)
            }
        }
    }

    func resume(using connection: BudConnection) async {
        guard control != nil else { return }
        await post("resume", using: connection) { _ in endControl() }
    }

    @discardableResult
    func sendControl(
        _ action: String, tabID: String, body: [String: Any], using connection: BudConnection
    ) async -> Bool {
        guard interactiveTabID == tabID else { return false }
        var payload = body
        payload["tab"] = tabID
        let timeout: TimeInterval = action == "navigate" ? 40 : 30
        return await post("control/\(action)", using: connection, body: payload, timeout: timeout) { _ in }
    }

    @discardableResult
    private func post(
        _ path: String, using connection: BudConnection, body: [String: Any]? = nil,
        timeout: TimeInterval = 10, apply: ([String: Any]) -> Void
    ) async -> Bool {
        guard !sending, !Task.isCancelled else { return false }
        sending = true
        problem = nil
        defer {
            sending = false
            requestRefresh()
        }
        do {
            var request = connection.request("POST", "browser/view/\(path)", json: body)
            request.timeoutInterval = timeout
            apply(try await connection.send(request))
            return true
        } catch ConnectionError.http(_, let detail) {
            problem = detail
        } catch {
            problem = "\(error)"
        }
        return false
    }
}

func normalizedBrowserPoint(_ point: CGPoint, in bounds: CGRect) -> CGPoint? {
    guard bounds.width > 0, bounds.height > 0 else { return nil }
    return CGPoint(
        x: min(1, max(0, (point.x - bounds.minX) / bounds.width)),
        y: min(1, max(0, (point.y - bounds.minY) / bounds.height))
    )
}

private struct BrowserPreviewInput: NSViewRepresentable {
    let click: (@MainActor (CGPoint) -> Void)?
    let scroll: @MainActor (CGSize) async -> Void

    func makeNSView(context: Context) -> Canvas { Canvas() }

    func updateNSView(_ view: Canvas, context: Context) {
        view.click = click
        view.scroll = scroll
    }

    final class Canvas: NSView {
        var click: (@MainActor (CGPoint) -> Void)?
        var scroll: (@MainActor (CGSize) async -> Void)?
        private var latestClick: CGPoint?
        private var ringTask: Task<Void, Never>?
        private var scrollTask: Task<Void, Never>?
        private var pendingScroll = CGSize.zero

        override var isFlipped: Bool { true }

        override func mouseDown(with event: NSEvent) {}

        override func mouseUp(with event: NSEvent) {
            let point = convert(event.locationInWindow, from: nil)
            guard let click, bounds.contains(point),
                  let normalized = normalizedBrowserPoint(point, in: bounds) else { return }
            latestClick = point
            needsDisplay = true
            ringTask?.cancel()
            ringTask = Task { [weak self] in
                try? await Task.sleep(for: .milliseconds(200))
                guard !Task.isCancelled, let self else { return }
                latestClick = nil
                needsDisplay = true
            }
            click(normalized)
        }

        override func draw(_ dirtyRect: NSRect) {
            guard let point = latestClick else { return }
            let ring = NSBezierPath(ovalIn: NSRect(x: point.x - 8, y: point.y - 8, width: 16, height: 16))
            NSColor.black.withAlphaComponent(0.4).setFill()
            ring.fill()
            NSColor.white.withAlphaComponent(0.8).setStroke()
            ring.lineWidth = 1.5
            ring.stroke()
        }

        override func scrollWheel(with event: NSEvent) {
            let scale: CGFloat = event.hasPreciseScrollingDeltas ? 1 : 16
            pendingScroll.width -= event.scrollingDeltaX * scale
            pendingScroll.height -= event.scrollingDeltaY * scale
            guard scrollTask == nil, pendingScroll != .zero else { return }
            scrollTask = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(80))
                    guard !Task.isCancelled, let self else { return }
                    let delta = pendingScroll
                    pendingScroll = .zero
                    if delta != .zero { await scroll?(delta) }
                    if pendingScroll == .zero {
                        scrollTask = nil
                        return
                    }
                }
            }
        }

        override func viewWillMove(toWindow newWindow: NSWindow?) {
            super.viewWillMove(toWindow: newWindow)
            if newWindow == nil {
                ringTask?.cancel()
                scrollTask?.cancel()
                scrollTask = nil
                pendingScroll = .zero
            }
        }
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
            if let control = state.control {
                switch control {
                case .interactive(let tabID, let title):
                    interactiveControl(tabID, title: title)
                case .chrome(let title, let handoffOpen):
                    chromeControl(title: title, handoffOpen: handoffOpen)
                }
            } else if let emptyState = state.emptyState {
                emptyContent(emptyState)
            } else if !state.groups.isEmpty {
                tabList.disabled(state.sending)
            } else if let problem = state.problem {
                guidance(problem) { connectionActions }
            } else {
                ProgressView().controlSize(.small).padding(14)
            }
            if (state.control != nil || state.emptyState == nil && !state.groups.isEmpty),
               let problem = state.problem {
                Text(problem)
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.errorInk)
                    .padding([.horizontal, .bottom], 14)
            }
        }
        .frame(maxHeight: .infinity, alignment: .topLeading)
        .background(Paper.canvas)
        .inspectorColumnWidth(min: state.expanded ? 470 : 265, ideal: state.expanded ? 650 : 340, max: 900)
        .task(id: state.refreshID) { [id = state.refreshID] in
            while !Task.isCancelled, id == state.refreshID {
                await state.refresh(using: selection.connection, id: id)
                try? await Task.sleep(for: .seconds(state.control == nil ? 3 : 1))
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
                if count > 0, state.control == nil {
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
        let title = tab.displayTitle
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
                        } else {
                            previewPlaceholder(issue)
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
                            Task { await state.takeOver(tab, using: selection.connection) }
                        }
                        .buttonStyle(CardButton(kind: .secondary))
                        .disabled(state.sending)
                    }
                }
                HStack(spacing: 6) {
                    let showHost = !tab.host.isEmpty && tab.host != title
                    if tab.id == state.activeTabID {
                        Text("Bud's tab")
                            .font(Paper.Typography.captionMedium)
                            .foregroundStyle(Paper.inkSecondary)
                        if showHost { Text("·").accessibilityHidden(true) }
                    }
                    if showHost {
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

    @ViewBuilder
    private func previewPlaceholder(_ issue: String?) -> some View {
        if let issue {
            VStack(spacing: 6) {
                Image(systemName: "macwindow")
                Text(issue).font(Paper.Typography.caption)
            }
            .foregroundStyle(Paper.inkMuted)
        } else {
            ProgressView().controlSize(.small)
        }
    }

    private func controlBar(title: String?) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text("You have control")
                    .font(Paper.Typography.label)
                    .foregroundStyle(Paper.inkPrimary)
                if let title {
                    Text(title)
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkSecondary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 0)
            Button("Give back control") { Task { await state.resume(using: selection.connection) } }
                .buttonStyle(CardButton(kind: .primary))
                .fixedSize()
                .disabled(state.sending)
        }
    }

    private func chromeControl(title: String?, handoffOpen: Bool) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title ?? "Chrome paused")
                .font(Paper.Typography.label)
                .lineLimit(1)
            ChromePrivateInput(
                pause: handoffOpen ? .handoff : .manual,
                busy: state.sending,
                onOpenFailure: { state.problem = "Could not open Chrome on this Mac." },
                onReturn: { Task { await state.resume(using: selection.connection) } }
            )
        }
        .padding(14)
    }

    private func interactiveControl(_ tabID: String, title: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            controlBar(title: title)
            let issue = state.previewIssues[tabID]
            let shape = RoundedRectangle(cornerRadius: Paper.radiusMD, style: .continuous)
            ZStack {
                Paper.recessed
                if let preview = state.previews[tabID] {
                    Image(nsImage: preview.image)
                        .resizable()
                        .scaledToFit()
                        .accessibilityLabel("Live preview of \(title)")
                        .overlay {
                            BrowserPreviewInput(
                                click: state.sending ? nil : { @MainActor point in
                                    Task {
                                        _ = await control("click", tabID, ["nx": point.x, "ny": point.y])
                                    }
                                },
                                scroll: { @MainActor delta in
                                    _ = await control("scroll", tabID, ["dx": delta.width, "dy": delta.height])
                                }
                            )
                        }
                } else {
                    previewPlaceholder(issue)
                }
            }
            .clipShape(shape)
            .overlay { PaperOutline(shape: shape, focused: false) }
            .id(tabID)
            if state.previews[tabID] != nil, let issue {
                Text(issue).font(Paper.Typography.caption).foregroundStyle(Paper.inkSecondary)
            }
            controlInputs(tabID)
                .disabled(state.sending)
        }
        .padding(14)
    }

    private func controlInputs(_ tabID: String) -> some View {
        VStack(spacing: 6) {
            HStack(spacing: 4) {
                controlField("Address", text: $state.address) { navigate(tabID) }
                controlButton("arrow.right", label: "Go") { navigate(tabID) }
            }
            HStack(spacing: 2) {
                controlField("Type into focused field...", text: $state.quickText) {
                    let text = state.quickText
                    guard !text.isEmpty else { return }
                    Task {
                        if await control("type", tabID, ["text": text]), state.quickText == text {
                            state.quickText = ""
                        }
                    }
                }
                quickKey("Enter", symbol: "return", tabID: tabID)
                quickKey("Backspace", symbol: "delete.left", tabID: tabID)
                quickKey("Tab", symbol: "arrow.right.to.line", tabID: tabID)
                quickKey("Escape", symbol: "escape", label: "Esc", tabID: tabID)
            }
        }
    }

    private func controlField(
        _ label: String, text: Binding<String>, submit: @escaping () -> Void
    ) -> some View {
        TextField(label, text: text)
            .textFieldStyle(.plain)
            .font(Paper.Typography.callout)
            .padding(.horizontal, 8)
            .frame(height: 26)
            .background(Paper.fillSubtle, in: RoundedRectangle(cornerRadius: Paper.radiusSM))
            .onSubmit(submit)
    }

    private func controlButton(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 12, weight: .medium))
        }
        .buttonStyle(IconButton())
        .help(label)
        .accessibilityLabel(label)
    }

    private func quickKey(_ key: String, symbol: String, label: String? = nil, tabID: String) -> some View {
        controlButton(symbol, label: label ?? key) {
            Task { await control("key", tabID, ["key": key]) }
        }
    }

    private func navigate(_ tabID: String) {
        let address = state.address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !address.isEmpty else { return }
        let url = address.contains("://") ? address : "https://\(address)"
        guard let parsed = URL(string: url), let scheme = parsed.scheme?.lowercased(),
              (scheme == "http" || scheme == "https"), let host = parsed.host(), !host.isEmpty else {
            state.problem = "Enter an HTTP or HTTPS address."
            return
        }
        Task { await control("navigate", tabID, ["url": parsed.absoluteString]) }
    }

    @discardableResult
    private func control(_ action: String, _ tabID: String, _ body: [String: Any]) async -> Bool {
        await state.sendControl(action, tabID: tabID, body: body, using: selection.connection)
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
