import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// The whole conversation in a normal Mac window, for long replies and earlier messages. The
/// pet's bubble is for a glance; this is for reading. While it's open, bud talks here and the
/// bubble stays shut; closing it hands back to the bubble.
///
/// A plain titled window, not a panel: it takes focus like any app window, so you can read
/// and type in it. The pin keeps it above other apps, as the pet is, for when you want both.
@MainActor @Observable
final class ConversationWindow: NSObject, NSWindowDelegate {
    static let shared = ConversationWindow()

    @ObservationIgnored private var window: NSWindow?
    private(set) var pinned = UserDefaults.standard.bool(forKey: "windowPinned")

    func show() {
        let window = self.window ?? make()
        self.window = window
        Companion.shared.windowOpen = true
        // bud has no Dock icon, so it has to bring itself forward for the window to take typing.
        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
    }

    /// Back to the small bubble beside the pet, which shows the latest reply.
    func collapse() {
        window?.close()
        Companion.shared.bubbleOpen = true
    }

    func chooseAttachments(_ add: @escaping ([URL]) -> Void) {
        guard let window else { return }
        let panel = NSOpenPanel()
        panel.prompt = "Attach"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = DraftAttachment.contentTypes
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.resolvesAliases = false
        panel.beginSheetModal(for: window) { response in
            if response == .OK { add(panel.urls) }
        }
    }

    func togglePin() {
        pinned.toggle()
        UserDefaults.standard.set(pinned, forKey: "windowPinned")
        window?.level = pinned ? .floating : .normal
    }

    func refreshBackground() {
        window?.backgroundColor = NSColor(Paper.canvas)
    }

    func windowWillClose(_ notification: Notification) {
        let companion = Companion.shared
        companion.windowOpen = false
        // Back to the bubble, with whatever bud last said or still needs.
        companion.bubbleOpen = !companion.shown.isEmpty || companion.runFailure != nil || companion.needsAttention
    }

    private func make() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 640),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "bud"
        // The canvas runs to the top edge, behind the traffic lights.
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(Paper.canvas)
        window.minSize = NSSize(width: 380, height: 420)
        window.level = pinned ? .floating : .normal
        window.delegate = self
        window.contentView = NSHostingView(rootView: ConversationView(companion: Companion.shared, window: self))
        if !window.setFrameUsingName("BudConversation") { window.center() }
        window.setFrameAutosaveName("BudConversation")
        return window
    }
}

final class TextZoom: ObservableObject {
    private static let key = "conversationTextZoom"
    private static let limits = -3...6
    private let defaults: UserDefaults
    @Published private(set) var delta: Int

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        delta = Self.clamped(defaults.integer(forKey: Self.key))
    }

    func increase() { set(delta + 1) }
    func decrease() { set(delta - 1) }
    func reset() { set(0) }

    private func set(_ value: Int) {
        let next = Self.clamped(value)
        guard next != delta else { return }
        delta = next
        defaults.set(next, forKey: Self.key)
    }

    private static func clamped(_ value: Int) -> Int {
        min(max(value, limits.lowerBound), limits.upperBound)
    }
}

/// The rhythm that separates speakers without labels: a wide gap before each of your
/// messages starts a new exchange, a shorter one joins bud's answer to your question.
private enum Rhythm {
    static let exchange: CGFloat = 52
    static let answer: CGFloat = 22
    /// The actions row under a reply: its spacing and height.
    static let actions: CGFloat = 8 + 24
    static let headerHeight: CGFloat = 40
}

struct ConversationScrollFollow: Equatable {
    private(set) var initialScrollPending = true
    private(set) var isFollowingBottom = true
    private(set) var userScrolling = false
    private var nearBottom = true

    var followsLayout: Bool { isFollowingBottom && !userScrolling && !initialScrollPending }
    var showsJumpButton: Bool { !isFollowingBottom && !nearBottom }

    mutating func takeInitialScroll() -> Bool {
        guard initialScrollPending else { return false }
        initialScrollPending = false
        return true
    }

    mutating func userBeganScrolling() {
        initialScrollPending = false
        userScrolling = true
    }

    mutating func geometryChanged(from old: ChatScrollGeometry, to new: ChatScrollGeometry) {
        nearBottom = new.nearBottom
        // Showing an earlier message can start moving while still inside the bottom threshold.
        if nearBottom, !old.nearBottom || userScrolling || !new.overflows {
            isFollowingBottom = true
        } else if userScrolling, old.offset != new.offset {
            isFollowingBottom = false
        }
    }

    mutating func userEndedScrolling() { userScrolling = false }

    mutating func revealEarlierMessage() {
        initialScrollPending = false
        isFollowingBottom = false
    }

    mutating func jumpToLatest() {
        initialScrollPending = false
        isFollowingBottom = true
        userScrolling = false
    }
}

@MainActor
private final class ScrollState: ObservableObject {
    @Published var follow = ConversationScrollFollow()
    @Published var position = ScrollPosition(idType: String.self)
    var showsJumpButton: Bool { follow.showsJumpButton }
    private var phase = ScrollPhase.idle
    private var wheelEnd: Task<Void, Never>?

    func phaseChanged(_ phase: ScrollPhase) {
        self.phase = phase
        switch phase {
        case .tracking, .interacting, .decelerating:
            if !follow.userScrolling { follow.userBeganScrolling() }
        case .idle:
            if wheelEnd == nil, follow.userScrolling { follow.userEndedScrolling() }
        default:
            break
        }
    }

    func wheelMoved() {
        if !follow.userScrolling { follow.userBeganScrolling() }
        wheelEnd?.cancel()
        wheelEnd = Task {
            do { try await Task.sleep(for: .milliseconds(150)) } catch { return }
            wheelEnd = nil
            if phase == .idle, follow.userScrolling { follow.userEndedScrolling() }
        }
    }
}

struct ChatScrollGeometry: Equatable {
    let content: CGSize
    let viewport: CGSize
    let offset: CGFloat
    let distanceFromBottom: CGFloat
    var overflows: Bool { content.height > viewport.height + 8 }
    var nearBottom: Bool { !overflows || distanceFromBottom <= 24 }
}

extension ChatScrollGeometry {
    init(_ geometry: ScrollGeometry) {
        self.init(content: geometry.contentSize, viewport: geometry.containerSize,
                  offset: geometry.visibleRect.minY,
                  distanceFromBottom: geometry.contentSize.height - geometry.visibleRect.maxY)
    }
}

enum ChatMessageID: Hashable {
    case turn(UUID), latest, queued(String)
}

struct ChatFindMatch: Equatable {
    let message: ChatMessageID
    let range: NSRange

    static func ranges(in text: String, query: String) -> [NSRange] {
        guard !query.isEmpty else { return [] }
        let text = text as NSString
        var matches: [NSRange] = []
        var start = 0
        while start < text.length {
            let range = text.range(
                of: query, options: .caseInsensitive, range: NSRange(location: start, length: text.length - start))
            guard range.location != NSNotFound, range.length > 0 else { break }
            matches.append(range)
            start = NSMaxRange(range)
        }
        return matches
    }

    static func index(
        _ messages: [(ChatMessageID, String)], query: String
    ) -> [Self] {
        messages.flatMap { message, text in
            ranges(in: text, query: query).map { Self(message: message, range: $0) }
        }
    }

    static func advance(from index: Int, count: Int, backward: Bool) -> Int? {
        guard count > 0 else { return nil }
        return (index + (backward ? count - 1 : 1)) % count
    }
}

enum ConversationEscapeTarget: Equatable {
    case shortcuts, find, queuedEdit, inspector, composer

    static func resolve(shortcuts: Bool, find: Bool, queuedEdit: Bool, inspector: Bool) -> Self {
        if shortcuts { return .shortcuts }
        if find { return .find }
        if queuedEdit { return .queuedEdit }
        if inspector { return .inspector }
        return .composer
    }
}

private final class FindInChat: ObservableObject {
    @Published var open = false {
        didSet { if open { navigation += 1 } }
    }
    @Published var query = "" {
        didSet {
            selected = nil
            navigation += 1
        }
    }
    @Published var selected: ChatFindMatch?
    @Published var navigation = 0
}

private final class QueueEdits: ObservableObject {
    struct Draft: Identifiable {
        let id: String
        var text: String
    }

    struct Recovery: Identifiable {
        let id = UUID()
        let text: String
    }

    @Published var draft: Draft?
    @Published var saving = false
    @Published var removing: Set<String> = []
    @Published var recoveries: [Recovery] = []

    func recover(_ draft: Draft) {
        recoveries.append(Recovery(text: draft.text))
        self.draft = nil
    }
}

private final class DraftPreviewSelection: ObservableObject {
    @Published var id: String?
}

private struct ConversationView: View {
    private static let logo: NSImage? = {
        if let icon = PetSprite.named("dog")?.icon(height: 22) {
            return icon
        }
        if let icns = Bundle.main.url(forResource: "AppIcon", withExtension: "icns"),
           let image = NSImage(contentsOf: icns) {
            return image
        }
        if Bundle.main.bundleURL.pathExtension == "app" {
            return NSImage(named: NSImage.applicationIconName)
        }
        return nil
    }()

    @Bindable var companion: Companion
    @Bindable var appearance = AppearanceStore.shared
    let window: ConversationWindow
    @FocusState private var typing: Bool
    @FocusState private var finding: Bool
    @StateObject private var scroll = ScrollState()
    @StateObject private var find = FindInChat()
    @StateObject private var queueEdits = QueueEdits()
    @StateObject private var shortcuts = Flag()
    @StateObject private var textZoom = TextZoom()
    @StateObject private var draftPreviewSelection = DraftPreviewSelection()
    @StateObject private var stopAllRequested = Flag()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.openSettings) private var openSettings

    /// Lines longer than this are hard to read, however wide the window gets.
    private let column: CGFloat = 640

    private var cardTransition: AnyTransition {
        reduceMotion
            ? .opacity
            : .scale(scale: 0.96).combined(with: .opacity)
    }

    @StateObject private var droppingAttachments = Flag()

    var body: some View {
        conversation
            .preferredColorScheme(appearance.mode.colorScheme)
            .onChange(of: appearance.revision) { _, _ in window.refreshBackground() }
            .background {
                Button("") { shortcuts.on.toggle() }
                    .keyboardShortcut("/", modifiers: .command)
                    .opacity(0)
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)
            }
            .sheet(isPresented: $shortcuts.on) { KeyboardShortcutsSheet() }
    }

    private var conversation: some View {
        let matches = find.open ? ChatFindMatch.index(searchableMessages, query: find.query) : []
        let activeIndex = find.selected.flatMap { matches.firstIndex(of: $0) } ?? 0
        let active = matches.indices.contains(activeIndex) ? matches[activeIndex] : nil
        let query = find.open ? find.query : ""

        return VStack(spacing: 0) {
            headerBar
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            if find.open {
                findBar(index: activeIndex, count: matches.count) { backward in
                    advanceFind(matches, from: activeIndex, backward: backward)
                }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(companion.turns.enumerated()), id: \.element.id) { index, turn in
                            let id = ChatMessageID.turn(turn.id)
                            TurnView(
                                turn: turn, companion: companion, focused: companion.focus == .turn(turn.id),
                                query: query, activeRange: active?.message == id ? active?.range : nil
                            )
                                .padding(.top, index == 0 ? 0 : turn.fromOwner ? Rhythm.exchange - Rhythm.actions : Rhythm.answer)
                                .id(turn.id.uuidString)
                        }
                        latest(query: query, active: active)
                            .padding(.top, companion.turns.isEmpty ? 0 : Rhythm.answer)
                            .id("live")
                        Color.clear.frame(height: 1).id("end")
                    }
                    .scrollTargetLayout()
                    .frame(maxWidth: column, alignment: .leading)
                    .padding(.horizontal, 32)
                    .padding(.top, 20)
                    .padding(.bottom, 44)
                    .frame(maxWidth: .infinity)
                }
                .overlay {
                    if companion.link == .connected, !companion.running, companion.turns.isEmpty,
                       companion.shown.isEmpty, companion.runFailure == nil {
                        welcome
                    }
                }
                .defaultScrollAnchor(.bottom, for: .initialOffset)
                .scrollPosition($scroll.position, anchor: .top)
                .onScrollPhaseChange { _, phase in scroll.phaseChanged(phase) }
                .onScrollGeometryChange(for: ChatScrollGeometry.self, of: ChatScrollGeometry.init) { old, new in
                    var follow = scroll.follow
                    follow.geometryChanged(from: old, to: new)
                    if follow != scroll.follow { scroll.follow = follow }
                    if follow.followsLayout, new.overflows, new.distanceFromBottom > 1,
                       old.content != new.content || old.viewport != new.viewport {
                        proxy.scrollTo("end", anchor: .bottom)
                    }
                }
                .background {
                    ConversationEvents(scroll: scroll.wheelMoved) { event in
                        handleKey(event, matches: matches, activeIndex: activeIndex)
                    }
                    .accessibilityHidden(true)
                }
                .overlay(alignment: .bottom) {
                    if scroll.showsJumpButton {
                        JumpButton {
                            scroll.follow.jumpToLatest()
                            withAnimation(reduceMotion ? nil : .easeOut(duration: 0.22)) {
                                proxy.scrollTo("end", anchor: .bottom)
                            }
                        }
                        .padding(.bottom, 12)
                        .frame(maxWidth: .infinity)
                        .background(alignment: .bottom) {
                            LinearGradient(
                                colors: [Paper.canvas.opacity(0), Paper.canvas.opacity(0.92)],
                                startPoint: .top,
                                endPoint: .bottom
                            )
                            .frame(height: 44)
                            .allowsHitTesting(false)
                        }
                        .transition(.opacity.combined(with: .move(edge: .bottom)))
                    }
                }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.16), value: scroll.showsJumpButton)
                .task(id: scroll.follow.initialScrollPending) {
                    guard scroll.follow.initialScrollPending else { return }
                    do { try await Task.sleep(for: .milliseconds(120)) } catch { return }
                    guard scroll.follow.takeInitialScroll() else { return }
                    if let focus = companion.focus {
                        scroll.follow.revealEarlierMessage()
                        scrollTo(focus, proxy)
                    } else {
                        proxy.scrollTo("end", anchor: .bottom)
                    }
                }
                .onChange(of: find.navigation) { _, _ in
                    if let active { revealFindMatch(active, proxy: proxy) }
                }
                // A source link pointed into this conversation: bring that message into view,
                // marked for a moment so your eye lands on it.
                .onChange(of: companion.focusRequest) { _, request in
                    guard let focus = companion.focus else { return }
                    scroll.follow.revealEarlierMessage()
                    withAnimation(reduceMotion ? nil : .spring(duration: 0.4, bounce: 0)) {
                        scrollTo(focus, proxy)
                    }
                    Task {
                        try? await Task.sleep(for: .seconds(2))
                        if companion.focusRequest == request { companion.focus = nil }
                    }
                }
            }
            .overlay(alignment: .bottom) { nudgePill }
            if !queueEdits.recoveries.isEmpty { recoverQueuedEdits }
            if companion.link != .connected { offlineStrip }
            field
        }
        .contentShape(Rectangle())
        .dropAttachments(into: companion, isTargeted: $droppingAttachments.on)
        .ignoresSafeArea(.container, edges: .top)
        .foregroundStyle(Paper.inkPrimary)
        .background {
            ZStack {
                Paper.canvas
                if appearance.theme == .paper {
                    DitherBackdrop()
                }
            }
            .ignoresSafeArea()
        }
        .environment(\.replyType, ReplyType(
            size: 14, lineSpacing: 5, blockSpacing: 14, itemSpacing: 6, compact: false,
            zoom: CGFloat(textZoom.delta)
        ))
        .inspector(isPresented: Binding(
            get: { companion.inspector != nil || draftPreview != nil },
            set: { if !$0 { closeInspector() } }
        )) {
            switch companion.inspector {
            case .attachment(let selection):
                AttachmentInspector(selection: selection, close: companion.closeInspector)
                    .id(selection.id)
            case .routine(let selection):
                RoutineInspector(
                    selection: selection,
                    refreshRevision: companion.routineRevision,
                    close: companion.closeInspector
                )
                .id(selection.id)
            case .browser(let selection):
                BrowserInspector(selection: selection, close: companion.closeInspector)
                    .id(selection.id)
            case .delivery(let selection):
                DesktopDeliveryInspector(selection: selection, close: companion.closeInspector)
                    .id(selection.id)
            case .report(let file):
                ReportInspector(file: file, close: closeInspector)
                    .id(file)
                    .inspectorColumnWidth(min: 300, ideal: 420, max: 700)
            case .source(let panel, let request):
                SourcePanelView(panel: panel, companion: companion)
                    .id(request)
                    .inspectorColumnWidth(min: 240, ideal: 300, max: 420)
            case nil:
                if let draftPreview {
                    DraftAttachmentPreview(
                        files: companion.draftAttachments, index: draftPreview,
                        select: { draftPreviewSelection.id = $0.id }, close: closeInspector
                    )
                }
            }
        }
        .environment(\.openURL, OpenURLAction { url in
            companion.openSource(url) ? .handled : .systemAction
        })
        .onAppear { typing = true }
        .onChange(of: companion.focusComposerNonce) { _, _ in typing = true }
        .onChange(of: companion.inputPurpose) { _, purpose in
            if purpose != .ask {
                draftPreviewSelection.id = nil
                typing = true
            }
        }
        .onChange(of: companion.inspector?.target) { _, target in
            if target != nil { draftPreviewSelection.id = nil }
        }
        .onChange(of: companion.draftAttachments.map(\.id)) { _, ids in
            if let id = draftPreviewSelection.id, !ids.contains(id) { draftPreviewSelection.id = nil }
        }
        .onChange(of: companion.replyingTo) { _, quote in
            if quote != nil { typing = true }
        }
        .onChange(of: companion.pendingMessages.map(\.id)) { _, ids in
            guard !queueEdits.saving, let draft = queueEdits.draft, !ids.contains(draft.id) else { return }
            queueEdits.recover(draft)
        }
    }

    private var draftPreview: Int? {
        guard let id = draftPreviewSelection.id else { return nil }
        return companion.draftAttachments.firstIndex { $0.id == id }
    }

    private func showDraftPreview(_ file: DraftAttachment) {
        companion.closeInspector()
        draftPreviewSelection.id = file.id
    }

    private func closeInspector() {
        draftPreviewSelection.id = nil
        companion.closeInspector()
    }

    private var welcome: some View {
        VStack(spacing: 10) {
            Text("How's it going?")
                .font(Fonts.prose(size: 22, weight: Fonts.OpticalWeight.medium))
                .tracking(-0.35)
                .foregroundStyle(Paper.inkPrimary)
            if companion.needsOnboarding {
                Text("Add a model to get started.")
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
                Button("Add a model") { OnboardingWindow.shared.show() }
                    .buttonStyle(CardButton(kind: .primary))
                connectToServer
            }
        }
    }

    private var connectToServer: some View {
        Button("Connect to a server…") {
            SettingsDraft.shared.connectToServer()
            openSettings()
        }
        .buttonStyle(.link)
        .font(Paper.Typography.caption)
        .tint(Paper.inkSecondary)
    }

    private var headerBar: some View {
        HStack(spacing: 2) {
            HStack(spacing: 7) {
                if let logo = Self.logo {
                    Image(nsImage: logo)
                        .resizable()
                        .interpolation(.high)
                        .scaledToFit()
                        .frame(width: 20, height: 20)
                        .accessibilityLabel("bud")
                }
                headerStatus
            }
            .padding(.leading, 76)
            Spacer(minLength: 8)
            headerTools
            Rectangle()
                .fill(Paper.hairline)
                .frame(width: 0.5, height: 14)
                .padding(.horizontal, 4)
            windowControls
        }
        .padding(.trailing, 10)
        .frame(height: Rhythm.headerHeight)
    }

    private var headerTools: some View {
        HStack(spacing: 2) {
            Button { companion.openRoutines() } label: {
                Image(systemName: "clock")
                    .font(.system(size: 12.5, weight: .medium))
            }
            .buttonStyle(IconButton(size: 28, active: routinesOpen))
            .background(routinesOpen ? Paper.fillSubtle : .clear, in: RoundedRectangle(cornerRadius: 6))
            .keyboardShortcut("r", modifiers: [.command, .shift])
            .help("Routines")
            .accessibilityLabel("Routines")

            Button(action: companion.openBrowserView) {
                Image(systemName: "macwindow")
                    .font(.system(size: 12.5, weight: .medium))
            }
            .buttonStyle(IconButton(size: 28, active: browserOpen))
            .background(browserOpen ? Paper.fillSubtle : .clear, in: RoundedRectangle(cornerRadius: 6))
            .keyboardShortcut("b", modifiers: [.command, .shift])
            .help("Browser")
            .accessibilityLabel("Browser")
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Conversation tools")
    }

    private var routinesOpen: Bool {
        if case .routine = companion.inspector { return true }
        return false
    }

    private var browserOpen: Bool {
        if case .browser = companion.inspector { return true }
        return false
    }

    @ViewBuilder private var headerStatus: some View {
        switch (companion.link, companion.status) {
        case (.connecting, _):
            ProgressView()
                .controlSize(.mini)
                .help(statusTooltip)
                .accessibilityLabel(statusTooltip)
        case (.problem, _), (.connected, .failed):
            Circle()
                .fill(Paper.errorInk)
                .frame(width: 6, height: 6)
                .help(statusTooltip)
                .accessibilityLabel(statusTooltip)
        case (.connected, _):
            EmptyView()
        }
    }

    private var statusTooltip: String {
        switch companion.link {
        case .connecting: return "Connecting…"
        case .problem(let detail): return detail
        case .connected: return companion.status.label
        }
    }

    private func scrollTo(_ focus: Focus, _ proxy: ScrollViewProxy) {
        switch focus {
        case .turn(let id): proxy.scrollTo(id.uuidString, anchor: .center)
        case .latest: proxy.scrollTo(ChatMessageID.latest, anchor: .center)
        }
    }

    private var searchableMessages: [(ChatMessageID, String)] {
        guard !find.query.isEmpty else { return [] }
        var messages = companion.turns.map { turn in
            let text = turn.fromOwner || turn.desktopJobID != nil
                ? ownerSearchText(turn.text)
                : MarkdownView.selectableText(Citations.linked(turn.text, sources: turn.sources))
            return (ChatMessageID.turn(turn.id), text)
        }
        if !companion.shown.isEmpty {
            messages.append((.latest, MarkdownView.selectableText(
                Citations.linked(companion.shown, sources: companion.replySources))))
        }
        messages += companion.pendingMessages.map { (.queued($0.id), ownerSearchText($0.text)) }
        return messages
    }

    private func ownerSearchText(_ text: String) -> String {
        let parts = Companion.unquoted(text)
        return [parts.quote, parts.text].compactMap { $0 }.joined(separator: "\n\n")
    }

    private func findBar(index: Int, count: Int, move: @escaping (Bool) -> Void) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 11))
                    .foregroundStyle(Paper.inkSecondary)
                    .accessibilityHidden(true)
                TextField("Find in chat", text: $find.query)
                    .textFieldStyle(.plain)
                    .font(Paper.Typography.callout)
                    .focused($finding)
                    .onAppear { finding = true }
                if !find.query.isEmpty {
                    Text(count == 0 ? "No matches" : "\(index + 1) / \(count)")
                        .font(Paper.Typography.ledgerMeta)
                        .foregroundStyle(Paper.inkSecondary)
                        .fixedSize()
                }
                compactIconButton("chevron.up", label: "Previous match") { move(true) }
                    .disabled(count == 0)
                compactIconButton("chevron.down", label: "Next match") { move(false) }
                    .disabled(count == 0)
                compactIconButton("xmark", label: "Close Find", action: closeFind)
            }
            .padding(.horizontal, 14)
            .frame(height: 34)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
        }
    }

    private func compactIconButton(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 10, weight: .medium))
        }
        .buttonStyle(IconButton(size: 23))
        .help(label)
        .accessibilityLabel(label)
    }

    private func advanceFind(_ matches: [ChatFindMatch], from index: Int, backward: Bool) {
        guard let next = ChatFindMatch.advance(from: index, count: matches.count, backward: backward) else { return }
        find.selected = matches[next]
        find.navigation += 1
    }

    private func revealFindMatch(_ match: ChatFindMatch, proxy: ScrollViewProxy) {
        scroll.follow.revealEarlierMessage()
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.24)) {
            switch match.message {
            case .turn(let id): proxy.scrollTo(id.uuidString, anchor: .center)
            case .latest, .queued: proxy.scrollTo(match.message, anchor: .center)
            }
        }
    }

    private func closeFind() {
        find.open = false
        finding = false
        typing = true
    }

    private func handleKey(
        _ event: NSEvent, matches: [ChatFindMatch], activeIndex: Int
    ) -> Bool {
        let modifiers = event.modifierFlags.intersection([.command, .shift, .option, .control])
        let key = event.charactersIgnoringModifiers?.lowercased()
        if (modifiers == .command && (key == "=" || key == "+"))
            || (modifiers == [.command, .shift] && (key == "+" || key == "=")) {
            textZoom.increase()
        } else if modifiers == .command, key == "-" {
            textZoom.decrease()
        } else if modifiers == .command, key == "0" {
            textZoom.reset()
        } else if modifiers == .command, key == "f" {
            if find.open {
                closeFind()
            } else {
                find.open = true
            }
        } else if find.open,
                  (key == "g" && (modifiers == .command || modifiers == [.command, .shift]))
                  || (finding && (event.keyCode == 36 || event.keyCode == 76)
                      && (modifiers.isEmpty || modifiers == .shift)) {
            advanceFind(matches, from: activeIndex, backward: modifiers.contains(.shift))
        } else if event.keyCode == 53, modifiers.isEmpty {
            if !event.isARepeat { dismissEscape() }
        } else {
            return false
        }
        return true
    }

    private func dismissEscape() {
        switch ConversationEscapeTarget.resolve(
            shortcuts: shortcuts.on, find: find.open, queuedEdit: queueEdits.draft != nil,
            inspector: companion.inspector != nil || draftPreview != nil
        ) {
        case .shortcuts:
            shortcuts.on = false
        case .find:
            closeFind()
        case .queuedEdit:
            cancelQueueEdit()
        case .inspector:
            closeInspector()
            typing = true
        case .composer:
            if Voice.shared.cancelActiveDictation() { return }
            if companion.replyingTo != nil || companion.inputPurpose != .ask {
                companion.inputPurpose = .ask
                companion.replyingTo = nil
            } else if companion.running {
                requestStop(allWork: false)
            } else {
                typing = false
            }
        }
    }

    private func reportSummary(_ text: String) -> String {
        // A notice can end partway through a link destination after truncation.
        let markdown = text.replacingOccurrences(
            of: #"!?\[([^\]\r\n]*)\]\([^)\r\n]*$"#, with: "$1", options: .regularExpression)
        let plain = MarkdownView.selectableText(markdown)
            .replacingOccurrences(of: "\u{2014}", with: " ")
            .split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
        return plain.isEmpty ? "Report ready" : plain
    }

    /// The reply in progress, or the last one, with what bud is doing and anything it needs.
    @ViewBuilder private func latest(query: String, active: ChatFindMatch?) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            if companion.running, let start = companion.runStart {
                Working(since: start, steps: companion.steps)
            } else if let work = companion.lastWork, !companion.shown.isEmpty || companion.runFailure != nil {
                WorkLine(work: work)
            }
            ForEach(companion.notices.filter { $0.report != nil }) { notice in
                if let report = notice.report {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(reportSummary(notice.text))
                            .font(Paper.Typography.callout)
                            .foregroundStyle(Paper.inkSecondary)
                            .lineLimit(2)
                        Button("Open report") { companion.openReport(report) }
                            .buttonStyle(.link)
                            .pointerStyle(.link)
                            .font(Paper.Typography.callout)
                            .accessibilityLabel("Open report \(report.lastPathComponent)")
                    }
                }
            }
            if companion.unsentSpeech != nil {
                UnsentSpeech(companion: companion).frame(maxWidth: 420)
            }
            if !companion.heldMessages.isEmpty {
                UnsentMessage(companion: companion).frame(maxWidth: 420)
            }
            if let failure = companion.runFailure {
                RunFailureView(
                    failure: failure, showTitle: true,
                    onEditAndResend: companion.canEditAndResendFailedTurn
                        ? { @MainActor in companion.editAndResendFailedTurn() } : nil,
                    priorExternalSteps: companion.failedTurnExternalSteps
                )
            }
            if !companion.shown.isEmpty {
                Reply(
                    text: companion.shown, sources: companion.replySources, companion: companion,
                    finished: !companion.running, query: query,
                    activeRange: active?.message == .latest ? active?.range : nil)
                    .messageHighlight(focused: companion.focus == .latest, found: active?.message == .latest)
                    .id(ChatMessageID.latest)
            }
            ForEach(companion.pendingMessages) { item in
                let id = ChatMessageID.queued(item.id)
                queuedMessage(item, query: query, activeRange: active?.message == id ? active?.range : nil)
                    .id(id)
            }
            if !companion.setups.isEmpty {
                SetupDeck(companion: companion)
                    .frame(maxWidth: 460, alignment: .leading)
                    .transition(cardTransition)
            }
            if !companion.approvals.isEmpty {
                // The card is the surface; a second panel around it read as a card in a card.
                ApprovalDeck(companion: companion)
                    .frame(maxWidth: 460, alignment: .leading)
                    .transition(cardTransition)
            } else if let receipt = companion.receipt {
                ReceiptLine(receipt: receipt)
                    .transition(.opacity)
            }
        }
        .animation(.spring(duration: 0.22, bounce: 0), value: companion.setups.map(\.id))
        .animation(.spring(duration: 0.22, bounce: 0), value: companion.approvals.first?.id)
    }

    private func queuedMessage(_ item: PendingMessage, query: String, activeRange: NSRange?) -> some View {
        let editing = queueEdits.draft?.id == item.id
        let busy = queueEdits.saving || queueEdits.removing.contains(item.id)
        return QueuedMessageRow(
            editing: editing, busy: busy, canEdit: queueEdits.draft == nil,
            edit: { queueEdits.draft = QueueEdits.Draft(id: item.id, text: item.text) },
            remove: { removeQueuedMessage(item.id) }
        ) {
            if editing {
                QueuedMessageEditor(
                    text: Binding(
                        get: { queueEdits.draft?.text ?? "" },
                        set: { queueEdits.draft?.text = $0 }
                    ),
                    saving: queueEdits.saving, save: saveQueueEdit, cancel: cancelQueueEdit
                )
            } else {
                TurnView(
                    turn: Turn(fromOwner: true, text: item.text), companion: companion,
                    query: query, activeRange: activeRange, selectable: false
                )
            }
        }
    }

    private func saveQueueEdit() {
        guard !queueEdits.saving, let draft = queueEdits.draft,
              !draft.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        queueEdits.saving = true
        Task {
            if await companion.editQueuedMessage(id: draft.id, text: draft.text) {
                queueEdits.draft = nil
            } else {
                queueEdits.recover(draft)
            }
            queueEdits.saving = false
            if !find.open, companion.inspector == nil { typing = true }
        }
    }

    private func cancelQueueEdit() {
        queueEdits.draft = nil
        typing = true
    }

    private func removeQueuedMessage(_ id: String) {
        guard queueEdits.removing.insert(id).inserted else { return }
        Task {
            if !(await companion.cancelQueuedMessage(id: id)),
               companion.pendingMessages.contains(where: { $0.id == id }) {
                companion.nudge("Could not remove the queued message.")
            }
            queueEdits.removing.remove(id)
        }
    }

    private var recoverQueuedEdits: some View {
        VStack(spacing: 6) {
            ForEach(queueEdits.recoveries) { recovery in
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Edits were not saved")
                            .font(Paper.Typography.captionMedium)
                        Text(recovery.text)
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkSecondary)
                            .lineLimit(2)
                    }
                    Spacer(minLength: 4)
                    Button("Copy edit") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(recovery.text, forType: .string)
                    }
                    .buttonStyle(CardButton(kind: .secondary))
                    compactIconButton("xmark", label: "Dismiss recovered edit") {
                        queueEdits.recoveries.removeAll { $0.id == recovery.id }
                    }
                }
                .padding(8)
                .background(Paper.recessed, in: RoundedRectangle(cornerRadius: Paper.radiusSM))
            }
        }
        .frame(maxWidth: column)
        .padding(.horizontal, 24)
    }

    private var nudgePill: some View {
        let latest = companion.notices.last { $0.report == nil }

        return ZStack {
            if let notice = latest {
                Text(notice.text)
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.inkSecondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                    .background(Paper.elevated, in: Capsule())
                    .overlay { Capsule().strokeBorder(Paper.borderStrong, lineWidth: 0.5) }
                    .shadow(color: .black.opacity(0.08), radius: 4, y: 2)
                    .padding(.horizontal, 24)
                    .id(notice.id)
                    .transition(reduceMotion ? .opacity : .opacity.combined(with: .offset(y: 4)))
            }
        }
        .padding(.bottom, scroll.showsJumpButton ? 50 : 4)
        .allowsHitTesting(false)
        .animation(.easeInOut(duration: 0.22), value: latest?.id)
        .task(id: latest?.id) {
            guard let latest else { return }
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            companion.dismissTransientNotices(ifLatest: latest.id)
        }
    }

    private var offlineStrip: some View {
        HStack(spacing: 6) {
            Text(companion.link == .connecting ? "Reconnecting…" : "Offline")
                .font(Paper.Typography.captionMedium)
                .foregroundStyle(Paper.inkSecondary)
                .help(statusTooltip)
                .lineLimit(1)
            Spacer(minLength: 0)
            if companion.link != .connecting {
                Button("Try again", action: companion.start)
                    .buttonStyle(CardButton(kind: .quiet))
            }
            Button("Connection settings") {
                SettingsTabState.shared.selected = .connection
                openSettings()
            }
            .buttonStyle(CardButton(kind: .quiet))
        }
        .padding(.horizontal, 10)
        .frame(maxWidth: column)
        .padding(.horizontal, 24)
    }

    private var hasDraft: Bool {
        let hasText = !companion.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        return hasText || (companion.inputPurpose == .ask && !companion.draftAttachments.isEmpty)
    }

    private var canSend: Bool {
        companion.link == .connected && hasDraft
            && (companion.inputPurpose != .ask || (!companion.readingAttachments && !companion.uploadingAttachments))
    }

    /// One field for everything: a grounded two-row composer with generous input space on top
    /// and a quiet utility strip below.
    private var field: some View {
        let remaining = DraftAttachment.maxCount - companion.draftAttachments.count
        let dropTargeted = droppingAttachments.on && (companion.canAddAttachments
            || (companion.inputPurpose == .ask && remaining <= 0))
        let composerShape = RoundedRectangle(cornerRadius: Paper.radiusComposer, style: .continuous)
        let composerFont = Fonts.sans(size: 14 + CGFloat(textZoom.delta))
        let prompt = companion.replyingTo != nil && companion.inputPurpose == .ask
            ? "Reply to this message…" : companion.inputPurpose.prompt

        return VStack(alignment: .leading, spacing: 0) {
            if dropTargeted {
                Text(remaining > 0 ? "Drop to attach · \(remaining) slots left" : "Attachment limit reached")
                    .font(Paper.Typography.captionMedium)
                    .foregroundStyle(Paper.inkSecondary)
                    .padding(.horizontal, 14)
                    .padding(.top, 8)
            }

            if let quote = companion.replyingTo {
                HStack(spacing: 8) {
                    RoundedRectangle(cornerRadius: 1)
                        .fill(Paper.borderStrong)
                        .frame(width: 2)
                    Text(quote)
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkSecondary)
                        .lineLimit(2)
                    Spacer(minLength: 4)
                    Button {
                        companion.replyingTo = nil
                    } label: {
                        Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
                    }
                    .buttonStyle(IconButton(size: 22))
                    .accessibilityLabel("Don't reply to this")
                }
                .fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 8)
                .padding(.trailing, 4)
                .padding(.vertical, 4)
                .background(Paper.recessed, in: RoundedRectangle(cornerRadius: Paper.radiusSM))
                .padding(.horizontal, 10)
                .padding(.top, 8)
            }

            if companion.inputPurpose == .ask {
                if !companion.draftAttachments.isEmpty {
                    DraftAttachmentChips(
                        files: companion.draftAttachments, remove: companion.removeAttachment,
                        onPreview: showDraftPreview
                    )
                    .padding(.horizontal, 14)
                    .padding(.top, 8)
                }
                if let summary = companion.attachmentDropSummary {
                    Text(summary)
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 14)
                        .padding(.top, 6)
                }
            }

            // Top row: multi-line text input with generous breathing room.
            ZStack(alignment: .topLeading) {
                if companion.draft.isEmpty {
                    Text(prompt)
                        .font(composerFont)
                        .foregroundStyle(Paper.placeholder)
                        .lineLimit(1)
                        .allowsHitTesting(false)
                }
                ChatInput(
                    title: "", text: $companion.draft, focus: $typing,
                    submit: { if canSend { companion.send() } },
                    onPasteImage: { companion.attachPastedImage($0) }
                )
                    .font(composerFont)
                    .lineLimit(1...6)
            }
            .padding(.horizontal, 14)
            .padding(.top, 11)
            .padding(.bottom, 5)

            stopStatus

            composerControls
        }
        .surface(
            composerShape,
            lift: 1,
            fill: Paper.elevated,
            border: dropTargeted ? Paper.inkPrimary : typing ? Paper.borderStrong : Paper.borderSubtle,
            borderWidth: dropTargeted || typing ? 1 : 0.5
        )
        .animation(.easeOut(duration: 0.15), value: typing)
        .animation(.easeOut(duration: 0.15), value: companion.replyingTo)
        .frame(maxWidth: column)
        .padding(.horizontal, 24)
        .padding(.bottom, 18)
        .padding(.top, 6)
    }

    private var composerControls: some View {
        let connected = companion.link == .connected
        let canStop = connected && !companion.stopping
        return HStack(spacing: 8) {
            if companion.inputPurpose == .ask {
                Button { window.chooseAttachments(companion.addAttachments) } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 14, weight: .medium))
                }
                .buttonStyle(IconButton(size: 28))
                .help("Attach files")
                .accessibilityLabel("Attach files")
                .disabled(!companion.canAddAttachments)
                if companion.readingAttachments || companion.uploadingAttachments {
                    ProgressView().controlSize(.small)
                        .accessibilityLabel(companion.readingAttachments ? "Reading files" : "Uploading files")
                }
            } else if companion.replyingTo == nil {
                Text("Esc to cancel")
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.inkMuted)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            ComposerVoiceControl(available: companion.inputPurpose == .ask && !companion.needsOnboarding)
            if companion.activeTasks.contains(where: { $0.finishedAt == nil }) {
                stopWorkMenu
            }
            if companion.running || companion.stopping {
                RoundButton(symbol: "stop.fill", label: "Stop response", size: 28) {
                    requestStop(allWork: false)
                }
                .disabled(!canStop)
                .opacity(canStop ? 1 : 0.35)
            }
            if connected, companion.needsOnboarding, companion.inputPurpose == .ask {
                Button("Add a model…") { OnboardingWindow.shared.show() }
                    .buttonStyle(CardButton(kind: .secondary))
                    .padding(.trailing, 4)
            } else if !connected || !companion.running || hasDraft {
                RoundButton(symbol: "arrow.up", label: "Send", size: 28, action: { companion.send() })
                    .disabled(!canSend)
                    .opacity(canSend ? 1 : 0.35)
            }
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 8)
        .padding(.top, 2)
    }

    @ViewBuilder private var stopStatus: some View {
        if companion.stopping {
            Text("Stopping…")
                .font(Paper.Typography.caption)
                .foregroundStyle(Paper.inkSecondary)
                .padding(.horizontal, 14)
        } else if let error = companion.stopError {
            HStack(spacing: 6) {
                Text(error)
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.errorInk)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button("Retry stop") { requestStop(allWork: stopAllRequested.on) }
                    .buttonStyle(CardButton(kind: .quiet))
                    .disabled(companion.link != .connected)
                    .fixedSize()
            }
            .padding(.horizontal, 14)
        }
    }

    private var stopWorkMenu: some View {
        Menu {
            Button("Stop all work") { requestStop(allWork: true) }
        } label: {
            Image(systemName: "ellipsis")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Paper.inkSecondary)
                .frame(width: 24, height: 26)
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .disabled(companion.link != .connected || companion.stopping)
        .help("Stop options")
        .accessibilityLabel("Stop options")
    }

    private func requestStop(allWork: Bool) {
        guard companion.link == .connected, !companion.stopping else { return }
        stopAllRequested.on = allWork
        if allWork {
            companion.stopAllWork()
        } else {
            companion.stopResponse()
        }
    }

    private var windowControls: some View {
        HStack(spacing: 2) {
            Button(action: window.togglePin) {
                Image(systemName: window.pinned ? "pin.fill" : "pin")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(window.pinned ? Paper.inkPrimary : Paper.inkSecondary)
            }
            .buttonStyle(IconButton(size: 28))
            .keyboardShortcut("p", modifiers: [.command, .shift])
            .help(window.pinned ? "Stop keeping on top" : "Keep on top of other windows")
            .accessibilityLabel(window.pinned ? "Unpin window" : "Keep window on top")
            Button(action: window.collapse) {
                Image(systemName: "arrow.down.right.and.arrow.up.left")
                    .font(.system(size: 12, weight: .medium))
            }
            .buttonStyle(IconButton(size: 28))
            .help("Back to the bubble")
            .accessibilityLabel("Collapse to the bubble")
        }
    }
}

private struct ComposerVoiceControl: View {
    let available: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let voice = Voice.shared
        let recording = voice.state == .listening
        if voice.state == .transcribing {
            ProgressView()
                .controlSize(.mini)
                .frame(width: 26, height: 26)
                .accessibilityLabel("Voice input")
                .accessibilityValue("Transcribing")
        } else if recording || available {
            HStack(spacing: 4) {
                if voice.microphoneDenied {
                    Button("Open Microphone Settings", action: voice.openMicrophoneSettings)
                        .buttonStyle(CardButton(kind: .quiet))
                        .lineLimit(1)
                } else if voice.microphoneUnavailable {
                    caption("No microphone connected")
                } else if recording, voice.noInputDetected {
                    caption("No input detected")
                }

                Button(action: voice.toggle) {
                    if recording {
                        levelBars(reduceMotion ? 0.65 : CGFloat(voice.level))
                    } else {
                        Image(systemName: "mic").font(.system(size: 12.5, weight: .medium))
                    }
                }
                .buttonStyle(IconButton(size: 26, active: recording))
                .background(recording ? Paper.fillHover : .clear, in: Capsule())
                .help(recording
                    ? "Click to stop and send (Esc cancels)"
                    : "Dictate a message (or hold \(voice.keyName))")
                .accessibilityLabel(recording ? "Stop dictation" : "Voice input")
            }
        }
    }

    private func caption(_ text: String) -> some View {
        Text(text)
            .font(Paper.Typography.caption)
            .foregroundStyle(Paper.inkSecondary)
            .lineLimit(1)
            .help(text)
    }

    private func levelBars(_ level: CGFloat) -> some View {
        HStack(spacing: 2) {
            ForEach([CGFloat(8), 14, 11], id: \.self) { peak in
                Capsule()
                    .fill(Paper.inkPrimary)
                    .frame(width: 2, height: 3 + (peak - 3) * level)
            }
        }
        .frame(height: 14)
        .accessibilityHidden(true)
    }
}

private struct DraftAttachmentPreview: View {
    let files: [DraftAttachment]
    let index: Int
    let select: (DraftAttachment) -> Void
    let close: () -> Void

    private let snippetLimit = 8192

    var body: some View {
        let file = files[index]
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 2) {
                Text("Attachment Preview")
                    .font(Paper.Typography.label)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 4)
                if files.count > 1 {
                    previewButton("chevron.left", label: "Previous attachment") { select(files[index - 1]) }
                        .disabled(index == 0)
                        .opacity(index == 0 ? 0.35 : 1)
                    previewButton("chevron.right", label: "Next attachment") { select(files[index + 1]) }
                        .disabled(index == files.count - 1)
                        .opacity(index == files.count - 1 ? 0.35 : 1)
                }
                previewButton("xmark", label: "Close attachment preview", action: close)
            }
            .padding(.horizontal, 14)
            .frame(height: Rhythm.headerHeight)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)

            VStack(alignment: .leading, spacing: 4) {
                Text(file.reference.name)
                    .font(Paper.Typography.callout)
                    .textSelection(.enabled)
                Text(metadata(for: file))
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.inkSecondary)
            }
            .padding(14)

            Group {
                if file.reference.type?.conforms(to: .image) == true, let image = NSImage(data: file.data) {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFit()
                        .accessibilityLabel(file.reference.name)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let type = file.reference.type, type.conforms(to: .text), type != .rtf {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(verbatim: String(decoding: file.data.prefix(snippetLimit), as: UTF8.self))
                                .font(Fonts.mono(size: 12))
                                .textSelection(.enabled)
                            if file.data.count > snippetLimit {
                                Text("Preview truncated")
                                    .font(Paper.Typography.caption)
                                    .foregroundStyle(Paper.inkSecondary)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 14)
            .id(file.id)
            Spacer(minLength: 0)
        }
        .background(Paper.canvas)
        .inspectorColumnWidth(min: 240, ideal: 320, max: 480)
    }

    private func metadata(for file: DraftAttachment) -> String {
        let size = ByteCountFormatter.string(fromByteCount: Int64(file.data.count), countStyle: .file)
        guard let type = file.reference.type?.localizedDescription else { return size }
        return "\(type) · \(size)"
    }

    private func previewButton(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 10, weight: .medium))
        }
        .buttonStyle(IconButton(size: 24))
        .help(label)
        .accessibilityLabel(label)
    }
}

private struct ConversationEvents: NSViewRepresentable {
    let scroll: () -> Void
    let key: (NSEvent) -> Bool

    func makeNSView(context: Context) -> Monitor { Monitor() }

    func updateNSView(_ view: Monitor, context: Context) {
        view.scroll = scroll
        view.key = key
    }

    final class Monitor: NSView {
        var scroll: (() -> Void)?
        var key: ((NSEvent) -> Bool)?
        private var monitor: Any?

        override func viewWillMove(toWindow newWindow: NSWindow?) {
            if let monitor {
                NSEvent.removeMonitor(monitor)
                self.monitor = nil
            }
            super.viewWillMove(toWindow: newWindow)
            guard newWindow != nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .scrollWheel, .leftMouseDown]) {
                [weak self] event in
                guard let self, let window, event.window === window, window.isKeyWindow else { return event }
                if event.type == .keyDown {
                    return key?(event) == true ? nil : event
                }
                guard bounds.contains(convert(event.locationInWindow, from: nil)) else { return event }
                if event.type == .scrollWheel, event.scrollingDeltaY != 0 {
                    scroll?()
                } else if event.type == .leftMouseDown, let content = window.contentView,
                          let frame = content.superview,
                          content.hitTest(frame.convert(event.locationInWindow, from: nil)) is NSScroller {
                    scroll?()
                }
                return event
            }
        }
    }
}

private struct QueuedMessageRow<Content: View>: View {
    private enum Action: Hashable { case edit, remove }

    let editing: Bool
    let busy: Bool
    let canEdit: Bool
    let edit: () -> Void
    let remove: () -> Void
    @ViewBuilder let content: () -> Content
    @StateObject private var hover = Flag()
    @FocusState private var focused: Action?

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            if editing {
                content()
            } else {
                content()
                    .contextMenu {
                        Button("Edit", action: edit).disabled(busy || !canEdit)
                        Button("Remove from queue", role: .destructive, action: remove).disabled(busy)
                    }
            }
            HStack(spacing: 8) {
                if !editing {
                    HStack(spacing: 8) {
                        Button("Edit", action: edit).disabled(busy || !canEdit)
                            .focused($focused, equals: .edit)
                        Button("Remove", action: remove).disabled(busy)
                            .focused($focused, equals: .remove)
                            .accessibilityLabel("Remove from queue")
                    }
                    .buttonStyle(.plain)
                    .font(Paper.Typography.captionMedium)
                    .foregroundStyle(Paper.inkSecondary)
                    .opacity(hover.on || focused != nil ? 1 : 0)
                    .allowsHitTesting(hover.on || focused != nil)
                }
                Label("Waiting to run", systemImage: "clock")
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.inkMuted)
            }
        }
        .onHover { hover.on = $0 }
    }
}

private struct QueuedMessageEditor: View {
    @Binding var text: String
    let saving: Bool
    let save: () -> Void
    let cancel: () -> Void
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .trailing, spacing: 8) {
            ChatInput(title: "Edit queued message", text: $text, focus: $focused, submit: save)
                .font(Paper.Typography.body)
                .lineLimit(2...6)
                .disabled(saving)
            HStack(spacing: 6) {
                Button("Cancel", action: cancel).buttonStyle(CardButton(kind: .secondary))
                Button("Save", action: save).buttonStyle(CardButton(kind: .secondary))
                    .disabled(saving || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(12)
        .background(Paper.recessed, in: RoundedRectangle(cornerRadius: 14))
        .overlay { RoundedRectangle(cornerRadius: 14).strokeBorder(Paper.borderStrong, lineWidth: 0.5) }
        .padding(.leading, 80)
        .onAppear { focused = true }
    }
}

private struct KeyboardShortcutsSheet: View {
    @Environment(\.dismiss) private var dismiss
    @Bindable private var appearance = AppearanceStore.shared

    var body: some View {
        let rows: [(label: String, key: String)] = [
            ("Ask bud", "⌘ K"),
            ("Dictate", Voice.shared.keyName),
            ("Keep on top", "⇧ ⌘ P"),
            ("Routines", "⇧ ⌘ R"),
            ("Browser", "⇧ ⌘ B"),
            ("Find in chat", "⌘ F"),
            ("Increase text size", "⌘ +"),
            ("Decrease text size", "⌘ -"),
            ("Reset text size", "⌘ 0"),
            ("Stop or cancel", "Esc"),
            ("Settings", "⌘ ,"),
            ("Shortcuts", "⌘ /"),
        ]

        return VStack(spacing: 0) {
            HStack {
                Text("Shortcuts")
                    .font(Paper.Typography.label)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 10, weight: .medium))
                }
                .buttonStyle(IconButton(size: 24))
                .keyboardShortcut(.cancelAction)
                .help("Close shortcuts")
                .accessibilityLabel("Close shortcuts")
            }
            .padding(.horizontal, 14)
            .frame(height: 40)

            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                Rectangle().fill(Paper.hairline).frame(height: 0.5)
                HStack {
                    Text(row.label).font(Paper.Typography.callout)
                    Spacer()
                    KeybindChip(row.key)
                }
                .padding(.horizontal, 16)
                .frame(height: 34)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(row.label)
                .accessibilityValue(row.key)
            }
        }
        .frame(width: 360)
        .foregroundStyle(Paper.inkPrimary)
        .background(Paper.canvas)
        .preferredColorScheme(appearance.mode.colorScheme)
    }
}

/// One message. Yours sits to the right on recessed paper with a 0.5pt hairline; bud's is plain
/// text at full width, the way a page reads, since it's what you came for.
private struct TurnView: View {
    let turn: Turn
    let companion: Companion
    /// A source link pointed here; the message is marked until the moment passes.
    var focused = false
    var query = ""
    var activeRange: NSRange? = nil
    var selectable = true
    @Environment(\.colorScheme) private var scheme
    @Environment(\.replyType) private var type

    var body: some View {
        content
            .messageHighlight(focused: focused, found: activeRange != nil)
            .animation(.easeOut(duration: 0.3), value: focused)
    }

    @ViewBuilder private var content: some View {
        if turn.fromOwner {
            let parts = Companion.unquoted(turn.text)
            let bubbleShape = RoundedRectangle(cornerRadius: 14, style: .continuous)
            HStack {
                Spacer(minLength: 80)
                VStack(alignment: .leading, spacing: 6) {
                    if let quote = parts.quote {
                        HStack(spacing: 8) {
                            RoundedRectangle(cornerRadius: 1).fill(Paper.borderStrong).frame(width: 2)
                            Text(highlighted(quote))
                                .font(Fonts.sans(size: type.scaledSize - 1))
                                .foregroundStyle(Paper.inkSecondary)
                                .lineLimit(activeRange == nil ? 3 : nil)
                        }
                        .fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(turn.attachments) { file in
                        SentAttachment(
                            file: file, selection: companion.selection(for: file)
                        ) { companion.openAttachment(file) }
                    }
                    if !parts.text.isEmpty {
                        let message = Text(highlighted(
                            parts.text, offset: parts.quote.map { $0.utf16.count + 2 } ?? 0
                        ))
                        .font(Fonts.sans(size: type.scaledSize))
                        .lineSpacing(3.5)
                        if selectable { message.textSelection(.enabled) } else { message }
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(scheme == .dark ? Paper.elevated : Paper.recessed, in: bubbleShape)
                .overlay(bubbleShape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5))
            }
        } else {
            VStack(alignment: .leading, spacing: 12) {
                if let work = turn.work { WorkLine(work: work) }
                if let card = turn.routine {
                    RoutineMessageCard(card: card) {
                        companion.openRoutines(card.id, occurrence: card.occurrenceID)
                    }
                }
                if let jobID = turn.desktopJobID {
                    let shape = RoundedRectangle(cornerRadius: Paper.radiusMD, style: .continuous)
                    Button {
                        companion.openDesktopDelivery(jobID)
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: "macwindow")
                                .font(.system(size: 13))
                                .foregroundStyle(Paper.inkSecondary)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(highlighted(turn.text))
                                    .font(Paper.Typography.callout)
                                    .foregroundStyle(Paper.inkPrimary)
                                    .lineLimit(activeRange == nil ? 2 : nil)
                                Text("Browser delivery")
                                    .font(Paper.Typography.caption)
                                    .foregroundStyle(Paper.inkMuted)
                            }
                            Spacer(minLength: 8)
                            HStack(spacing: 4) {
                                Text("Open").font(Paper.Typography.captionMedium)
                                Image(systemName: "chevron.right").font(.system(size: 9))
                            }
                            .foregroundStyle(Paper.inkSecondary)
                        }
                        .padding(10)
                        .frame(maxWidth: 360, alignment: .leading)
                    }
                    .buttonStyle(PaperHit(shape: shape, fill: Paper.elevated))
                    .overlay { shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5).allowsHitTesting(false) }
                    .help("Open browser delivery")
                    .accessibilityLabel(turn.text)
                } else if !turn.text.isEmpty {
                    Reply(
                        text: turn.text, sources: turn.sources, companion: companion, finished: true,
                        query: query, activeRange: activeRange
                    )
                }
            }
        }
    }

    private func highlighted(_ text: String, offset: Int = 0) -> AttributedString {
        var result = AttributedString(text)
        for range in ChatFindMatch.ranges(in: text, query: query) {
            guard let source = Range(range, in: text),
                  let start = AttributedString.Index(source.lowerBound, within: result),
                  let end = AttributedString.Index(source.upperBound, within: result) else { continue }
            let isActive = activeRange == NSRange(location: range.location + offset, length: range.length)
            result[start..<end].backgroundColor = isActive ? Paper.accent.opacity(0.24) : Paper.fillSubtle
        }
        return result
    }
}

private extension View {
    func messageHighlight(focused: Bool, found: Bool) -> some View {
        background {
            if focused || found {
                RoundedRectangle(cornerRadius: Paper.radiusMD)
                    .fill(Paper.accentFill)
                    .overlay {
                        if found {
                            RoundedRectangle(cornerRadius: Paper.radiusMD)
                                .strokeBorder(Paper.borderFocus, lineWidth: 0.5)
                        }
                    }
                    .padding(-6)
                    .allowsHitTesting(false)
            }
        }
    }
}

/// bud's words, with copy and reply under them when you hover: the actions are there when you
/// reach for them and out of the way when you read.
private struct Reply: View {
    private enum Action: Hashable { case copy, reply }

    let text: String
    /// What the reply's `[S1]` tags cite; they show as dates that open the cited message.
    var sources: [String: Source] = [:]
    let companion: Companion
    /// A reply still streaming has no actions yet.
    let finished: Bool
    var query = ""
    var activeRange: NSRange? = nil
    @StateObject private var hovered = Flag()
    @StateObject private var copied = CopyFeedback()
    @StateObject private var selection = ReplyTextSelection()
    @FocusState private var focusedAction: Action?

    private var showsActions: Bool { hovered.on || copied.on || focusedAction != nil }

    private final class CopyFeedback: ObservableObject {
        @Published var id: UUID?
        var on: Bool { id != nil }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            MarkdownView(
                Citations.linked(text, sources: sources), rich: finished,
                selection: selection, query: query, activeRange: activeRange,
                onQuote: quote
            )
            if finished {
                HStack(spacing: 2) {
                    action(.copy, symbol: "doc.on.doc", label: copied.on ? "Copied" : "Copy") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(
                            Citations.plain(text, sources: sources), forType: .string)
                        copied.id = UUID()
                    }
                    action(.reply, symbol: "arrowshape.turn.up.left", label: "Reply") {
                        quote(selection.text ?? Citations.plain(text, sources: sources))
                    }
                    if copied.on {
                        Label("Copied", systemImage: "checkmark")
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.approveInk)
                            .padding(.leading, 4)
                            .accessibilityHidden(true)
                            .transition(.opacity)
                    }
                }
                .padding(.leading, -5)
                .frame(height: 24)
                .animation(.easeOut(duration: 0.15), value: copied.on)
                .task(id: copied.id) {
                    guard let id = copied.id else { return }
                    try? await Task.sleep(for: .seconds(1.2))
                    guard !Task.isCancelled, copied.id == id else { return }
                    copied.id = nil
                }
            }
        }
        .contentShape(Rectangle())
        .onHover { hovered.on = $0 }
    }

    private func quote(_ text: String) {
        companion.replyingTo = text
    }

    private func action(
        _ action: Action, symbol: String, label: String, run: @escaping () -> Void
    ) -> some View {
        Button(action: run) {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .medium))
                .contentTransition(.symbolEffect(.replace))
                .opacity(showsActions ? 1 : 0)
                .animation(focusedAction == nil ? .easeOut(duration: 0.15) : nil, value: showsActions)
        }
        .buttonStyle(IconButton(size: 24))
        .focused($focusedAction, equals: action)
        .focusEffectDisabled()
        .overlay {
            if focusedAction == action {
                RoundedRectangle(cornerRadius: Paper.radiusXS)
                    .strokeBorder(Paper.inkPrimary, lineWidth: 1)
                    .allowsHitTesting(false)
            }
        }
        .help(label)
        .accessibilityLabel(label)
    }
}

/// While bud works: an activity indicator, elapsed time, and live steps.
private struct Working: View {
    let since: Date
    let steps: [Step]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private static let verbs = [
        "Sniffing around", "Following the trail", "Fetching details", "Digging in", "Percolating",
        "Nosing through it", "Piecing it together", "Tracking it down", "Chewing on it",
        "Connecting the dots",
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                dots
                TimelineView(.periodic(from: since, by: 0.5)) { context in
                    let duration = max(0, context.date.timeIntervalSince(since))
                    let offset = abs(Int(since.timeIntervalSinceReferenceDate * 1000) % Self.verbs.count)
                    let verb = Self.verbs[(offset + Int(duration / 3.5)) % Self.verbs.count]
                    let count = steps.count
                    let stepSuffix = count == 0 ? "" : " · \(count) \(count == 1 ? "step" : "steps")"

                    HStack(spacing: 6) {
                        Text(verb)
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkSecondary)
                            .contentTransition(.opacity)
                            .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: verb)
                        Text("\(elapsed(Int(duration)))\(stepSuffix)")
                            .font(Paper.Typography.ledgerMeta)
                            .foregroundStyle(Paper.inkMuted)
                    }
                }
            }
            if !steps.isEmpty {
                StepList(steps: steps, live: true)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .animation(.spring(duration: 0.25, bounce: 0), value: steps.count)
    }

    private var dots: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { context in
            let phase = context.date.timeIntervalSince(since) * 2 * .pi / 1.2
            HStack(spacing: 3) {
                ForEach(0..<3) { index in
                    let wave = reduceMotion ? 0 : sin(phase - Double(index) * .pi / 3)
                    Circle()
                        .fill(Paper.inkPrimary)
                        .frame(width: 3.5, height: 3.5)
                        .offset(y: -2 * wave)
                        .opacity(reduceMotion ? 1 : 0.78 + 0.22 * wave)
                }
            }
            .frame(height: 8)
        }
        .accessibilityHidden(true)
    }
}

/// A finished run folded to one muted line, "Worked for 34s · 4 steps ›", that opens the steps.
private struct WorkLine: View {
    let work: Work
    @StateObject private var open = Flag()
    @StateObject private var hovered = Flag()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if work.steps.isEmpty {
                summary
            } else {
                Button { open.on.toggle() } label: {
                    HStack(spacing: 5) {
                        summary
                        Image(systemName: "chevron.right")
                            .font(.system(size: 8, weight: .medium))
                            .foregroundStyle(hovered.on ? Paper.inkPrimary : Paper.inkMuted)
                            .rotationEffect(.degrees(open.on ? 90 : 0))
                    }
                    .padding(.horizontal, 5)
                    .padding(.vertical, 2)
                    .background(
                        hovered.on ? Paper.fillHover : .clear,
                        in: RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .pointerStyle(.link)
                .onHover { hovered.on = $0 }
                .padding(.leading, -5)
                .accessibilityHint(open.on ? "Hide bud's steps" : "Show bud's steps")
                if open.on {
                    StepList(steps: work.steps, live: false, limit: nil)
                        .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .animation(.spring(duration: 0.25, bounce: 0), value: open.on)
    }

    private var summary: some View {
        let count = work.steps.count
        let steps = count == 0 ? "" : " · \(count) \(count == 1 ? "step" : "steps")"
        return Text("Worked for \(elapsed(work.seconds))\(steps)")
            .font(Fonts.sans(size: 11.5, tabularDigits: true))
            .foregroundStyle(hovered.on ? Paper.inkPrimary : Paper.inkSecondary)
    }
}

/// "8s", "1m 05s", "1h 02m": compact monotonic timer shape so a ticking timer doesn't jitter.
private func elapsed(_ seconds: Int) -> String {
    let seconds = max(0, seconds)
    if seconds < 60 { return "\(seconds)s" }
    let minutes = seconds / 60
    if minutes < 60 { return "\(minutes)m \(String(format: "%02d", seconds % 60))s" }
    return "\(minutes / 60)h \(String(format: "%02d", minutes % 60))m"
}

/// Back to the newest message, when you've meaningfully scrolled up to read earlier turns.
private struct JumpButton: View {
    let action: () -> Void
    @StateObject private var hovered = Flag()

    var body: some View {
        Button(action: action) {
            Image(systemName: "arrow.down")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(hovered.on ? Paper.inkPrimary : Paper.inkSecondary)
                .frame(width: 30, height: 30)
                .surface(
                    Capsule(),
                    lift: 1.0,
                    fill: hovered.on ? Paper.overlay : Paper.elevated,
                    border: Paper.borderStrong,
                    borderWidth: 0.5
                )
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .pointerStyle(.link)
        .onHover { hovered.on = $0 }
        .help("Jump to latest")
        .accessibilityLabel("Jump to latest")
    }
}

private struct ReportInspector: View {
    private final class Report: ObservableObject {
        enum State {
            case loading, ready(String), unreadable
        }

        @Published var state: State = .loading
    }

    let file: URL
    let close: () -> Void
    @StateObject private var report = Report()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 4) {
                Text("Report")
                    .font(Paper.Typography.label)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button { NSWorkspace.shared.open(file) } label: {
                    Image(systemName: "arrow.up.forward.square")
                }
                .buttonStyle(IconButton(size: 28))
                .help("Open in default app")
                .accessibilityLabel("Open in default app")
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(IconButton(size: 26))
                    .help("Close report")
                    .accessibilityLabel("Close report")
            }
            .font(.system(size: 12, weight: .medium))
            .padding(.horizontal, 14)
            .frame(height: Rhythm.headerHeight)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            switch report.state {
            case .loading:
                ProgressView().controlSize(.small).padding(16)
            case .ready(let contents):
                ScrollView {
                    MarkdownView(contents, rich: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(16)
                }
            case .unreadable:
                Text("This report could not be read.")
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
                    .padding(16)
            }
        }
        .frame(maxHeight: .infinity, alignment: .topLeading)
        .background(Paper.canvas)
        .task(id: file) { [file] in
            let contents = await Task.detached {
                try? String(contentsOf: file, encoding: .utf8)
            }.value
            guard !Task.isCancelled else { return }
            report.state = contents.map { .ready($0) } ?? .unreadable
        }
    }
}

/// A cited message or workspace file beside the chat.
private struct SourcePanelView: View {
    let panel: SourcePanel
    let companion: Companion

    private var subtitle: String {
        if case .workspace = panel.target {
            return panel.date.isEmpty ? "Workspace file" : "Workspace file · \(panel.date)"
        }
        return companion.chatFocus(for: panel) == nil ? "Earlier conversation" : "This conversation"
    }

    private var headline: String {
        if case .workspace(let path) = panel.target { return path }
        return panel.date.isEmpty ? " " : panel.date
    }

    private var localWorkspaceFile: URL? {
        guard case .workspace(let path) = panel.target, panel.document != nil,
              !path.hasPrefix("/"), !path.split(separator: "/").contains("..")
        else { return nil }
        let settings = BudSettings.load()
        guard !settings.savedRemote, let origin = URL(string: settings.url),
              origin.scheme == "http", BudConnection.safeOrigin(origin)
        else { return nil }
        let home = settings.home.isEmpty
            ? FileManager.default.homeDirectoryForCurrentUser.appending(path: ".bud")
            : BudConnection.resolvedHomeURL(home: settings.home)
        let workspace = home.appending(path: "workspace").resolvingSymlinksInPath()
        let file = workspace.appending(path: path).resolvingSymlinksInPath()
        guard file.pathComponents.count > workspace.pathComponents.count,
              file.pathComponents.starts(with: workspace.pathComponents),
              (try? file.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true
        else { return nil }
        return file
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(subtitle)
                        .font(Paper.Typography.ledgerMeta)
                        .foregroundStyle(Paper.inkSecondary)
                        .lineLimit(1)
                    Text(headline)
                        .font(Paper.Typography.h3)
                        .tracking(Paper.Typography.h3Tracking)
                        .lineLimit(1)
                }
                Spacer()
                if localWorkspaceFile != nil {
                    Button {
                        if let file = localWorkspaceFile {
                            NSWorkspace.shared.activateFileViewerSelecting([file])
                        }
                    } label: {
                        Image(systemName: "arrow.up.right.square").font(.system(size: 12))
                    }
                    .buttonStyle(IconButton(size: 26))
                    .help("Reveal in Finder")
                    .accessibilityLabel("Reveal in Finder")
                }
                Button(action: companion.closeInspector) {
                    Image(systemName: "xmark").font(.system(size: 10, weight: .semibold))
                }
                .buttonStyle(IconButton(size: 24))
                .help("Close")
                .accessibilityLabel("Close source")
            }
            .padding(.horizontal, 16)
            .frame(height: Rhythm.headerHeight)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            if let problem = panel.problem {
                Text(problem)
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
                    .padding(16)
                Spacer()
            } else if panel.loading {
                ProgressView().controlSize(.small).frame(maxWidth: .infinity).padding(24)
                Spacer()
            } else if let document = panel.document {
                ScrollView {
                    MarkdownView(document)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(16)
                }
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 18) {
                            ForEach(panel.turns) { turn in
                                VStack(alignment: .leading, spacing: 8) {
                                    SourceTurn(turn: turn, cited: turn.id == panel.citedTurn?.id)
                                    if case .turn(let sourceSession, _) = panel.target {
                                        ForEach(turn.attachments) { file in
                                            SentAttachment(
                                                file: file,
                                                selection: companion.selection(for: file, in: sourceSession)
                                            ) { companion.openAttachment(file, in: sourceSession) }
                                        }
                                    }
                                }
                                .id(turn.id)
                            }
                        }
                        .padding(16)
                    }
                    .task {
                        guard let cited = panel.citedTurn else { return }
                        await Task.yield()
                        proxy.scrollTo(cited.id, anchor: .center)
                    }
                }
            }
        }
        .foregroundStyle(Paper.inkPrimary)
        .background(Paper.canvas)
        .overlay(alignment: .leading) {
            Rectangle().fill(Paper.hairline).frame(width: 0.5).ignoresSafeArea()
        }
    }
}

private struct SourceTurn: View {
    let turn: Turn
    let cited: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            RoundedRectangle(cornerRadius: 1)
                .fill(cited ? Paper.accent.opacity(0.6) : .clear)
                .frame(width: 2)
            VStack(alignment: .leading, spacing: 4) {
                Text(turn.fromOwner ? "You" : "bud")
                    .font(Paper.Typography.captionMedium)
                    .foregroundStyle(Paper.inkSecondary)
                MarkdownView(Citations.linked(turn.text, sources: turn.sources))
                    .textSelection(.enabled)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .opacity(cited ? 1 : 0.7)
    }
}
