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

/// Replies in the window use 14 pt prose with relaxed line spacing.
private let pageType = ReplyType(
    size: 14, lineSpacing: 5, blockSpacing: 14, itemSpacing: 6, compact: false)

/// The rhythm that separates speakers without labels: a wide gap before each of your
/// messages starts a new exchange, a shorter one joins bud's answer to your question.
private enum Rhythm {
    static let exchange: CGFloat = 52
    static let answer: CGFloat = 22
    /// The actions row under a reply: its spacing and height.
    static let actions: CGFloat = 8 + 24
    static let headerHeight: CGFloat = 40
}

private final class ScrollState: ObservableObject {
    @Published private(set) var machine = ScrollFollowMachine()

    var atBottom: Bool { machine.atBottom }
    var showsJumpButton: Bool { machine.showsJumpButton }

    func update(nearBottom: Bool) {
        var next = machine
        next.reduce(.nearBottomChanged(nearBottom, now: .now))
        if next != machine { machine = next }
    }

    func jumpToBottom() {
        machine.reduce(.jumpToBottom(now: .now))
    }
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
    @StateObject private var restoreTyping = Flag()
    @StateObject private var scroll = ScrollState()
    @StateObject private var history = Flag()
    @StateObject private var menuHover = Flag()
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
        conversation.sheet(isPresented: $history.on, content: historySheet)
            .preferredColorScheme(appearance.mode.colorScheme)
            .onChange(of: appearance.revision) { _, _ in window.refreshBackground() }
    }

    private var conversation: some View {
        VStack(spacing: 0) {
            headerBar
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(companion.turns.enumerated()), id: \.element.id) { index, turn in
                            TurnView(turn: turn, companion: companion, focused: companion.focus == .turn(turn.id))
                                .padding(.top, index == 0 ? 0 : turn.fromOwner ? Rhythm.exchange - Rhythm.actions : Rhythm.answer)
                        }
                        latest
                            .padding(.top, companion.turns.isEmpty ? 0 : Rhythm.answer)
                        Color.clear.frame(height: 1).id("end")
                    }
                    .frame(maxWidth: column, alignment: .leading)
                    .padding(.horizontal, 32)
                    .padding(.top, 20)
                    .padding(.bottom, 44)
                    .frame(maxWidth: .infinity)
                }
                .overlay {
                    if companion.turns.isEmpty, companion.shown.isEmpty, companion.runFailure == nil {
                        connectionStatus
                    }
                }
                // Opens at the latest message, and stays there as a reply streams in, unless
                // you've scrolled up to read something earlier.
                .defaultScrollAnchor(.bottom)
                .defaultScrollAnchor(.bottom, for: .sizeChanges)
                .onScrollGeometryChange(for: Bool.self) { geometry in
                    ScrollFollowMachine.isNearBottom(
                        contentHeight: geometry.contentSize.height,
                        containerHeight: geometry.containerSize.height,
                        visibleMaxY: geometry.visibleRect.maxY
                    )
                } action: { _, nearBottom in
                    scroll.update(nearBottom: nearBottom)
                }
                .overlay(alignment: .bottom) {
                    if scroll.showsJumpButton {
                        JumpButton {
                            scroll.jumpToBottom()
                            withAnimation(.easeOut(duration: 0.14)) {
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
                .animation(.easeOut(duration: 0.16), value: scroll.atBottom)
                .onAppear {
                    // Replies finish laying out a moment after the first pass, which leaves the
                    // anchor short of the end; settle on the latest message once they have.
                    Task {
                        try? await Task.sleep(for: .milliseconds(120))
                        if let focus = companion.focus { scrollTo(focus, proxy) } else {
                            proxy.scrollTo("end", anchor: .bottom)
                        }
                    }
                }
                // A source link pointed into this conversation: bring that message into view,
                // marked for a moment so your eye lands on it.
                .onChange(of: companion.focusRequest) { _, request in
                    guard let focus = companion.focus else { return }
                    withAnimation(.spring(duration: 0.4, bounce: 0)) { scrollTo(focus, proxy) }
                    Task {
                        try? await Task.sleep(for: .seconds(2))
                        if companion.focusRequest == request { companion.focus = nil }
                    }
                }
            }
            field
        }
        .contentShape(Rectangle())
        .dropAttachments(into: companion, isTargeted: $droppingAttachments.on)
        .accessibilityHidden(companion.citedConversation != nil)
        .allowsHitTesting(companion.citedConversation == nil)
        .overlay {
            if let panel = companion.citedConversation {
                CitedConversationView(panel: panel, companion: companion)
                    .id(panel.target)
            }
        }
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
        .environment(\.replyType, pageType)
        .environment(\.openURL, OpenURLAction { url in
            companion.openSource(url) ? .handled : .systemAction
        })
        .inspector(isPresented: Binding(
            get: { companion.inspector != nil },
            set: { if !$0 { companion.closeInspector() } }
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
            case .source(let panel, let request):
                SourcePanelView(panel: panel, companion: companion)
                    .id(request)
                    .inspectorColumnWidth(min: 240, ideal: 300, max: 420)
            case nil:
                EmptyView()
            }
        }
        .onAppear { typing = true }
        .onChange(of: companion.citedConversation != nil) { _, reading in
            if reading {
                restoreTyping.on = typing
                typing = false
            } else if restoreTyping.on {
                typing = true
                restoreTyping.on = false
            }
        }
        .onChange(of: companion.inputPurpose) { _, purpose in
            if purpose != .ask, companion.citedConversation == nil { typing = true }
        }
        .onChange(of: companion.replyingTo) { _, quote in
            if quote != nil { typing = true }
        }
    }

    private func historySheet() -> some View {
        ConversationHistory(show: { url in
            history.on = false
            _ = companion.openSource(url)
        }, close: { history.on = false })
    }

    @ViewBuilder private var connectionStatus: some View {
        switch companion.link {
        case .connecting:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Connecting to Bud…")
            }
            .font(Paper.Typography.callout)
            .foregroundStyle(Paper.inkSecondary)
        case .problem:
            VStack(spacing: 10) {
                Text("Couldn’t open conversations.")
                    .font(Paper.Typography.body)
                Button("Try Again", action: companion.start)
                    .buttonStyle(CardButton(kind: .secondary))
                connectToServer
            }
        case .connected:
            if !companion.running {
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
            overviewMenu
            Rectangle()
                .fill(Paper.hairline)
                .frame(width: 0.5, height: 14)
                .padding(.horizontal, 4)
            windowControls
        }
        .padding(.trailing, 10)
        .frame(height: Rhythm.headerHeight)
    }

    private var overviewMenu: some View {
        Menu {
            Button { history.on = true } label: {
                Label("Conversations…", systemImage: "text.bubble")
            }
            Button { companion.openRoutines() } label: {
                Label("Routines", systemImage: routinesOpen ? "checkmark" : "clock")
            }
            Button(action: companion.openBrowserView) {
                Label("Browser", systemImage: browserOpen ? "checkmark" : "macwindow")
            }
        } label: {
            Image(systemName: "ellipsis")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(menuHover.on ? Paper.inkPrimary : Paper.inkSecondary)
                .frame(width: 28, height: 28)
                .background(
                    menuHover.on ? Paper.fillHover : .clear,
                    in: RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous))
                .contentShape(RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous))
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .menuOrder(.fixed)
        .fixedSize()
        .pointerStyle(.link)
        .onHover { menuHover.on = $0 }
        .help("Conversation tools")
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
        case .turn(let id): proxy.scrollTo(id, anchor: .center)
        case .latest: proxy.scrollTo("latest-reply", anchor: .center)
        }
    }

    /// The reply in progress, or the last one, with what bud is doing and anything it needs.
    @ViewBuilder private var latest: some View {
        VStack(alignment: .leading, spacing: 12) {
            if companion.running, let start = companion.runStart {
                Working(since: start, steps: companion.steps)
            } else if let work = companion.lastWork, !companion.shown.isEmpty || companion.runFailure != nil {
                WorkLine(work: work)
            }
            ForEach(companion.notices) { notice in
                VStack(alignment: .leading, spacing: 4) {
                    Label(notice.text, systemImage: "arrow.turn.down.right")
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkSecondary)
                    if let report = notice.report { OpenReport(file: report) }
                }
            }
            if companion.unsentSpeech != nil {
                UnsentSpeech(companion: companion).frame(maxWidth: 420)
            }
            if !companion.heldMessages.isEmpty {
                UnsentMessage(companion: companion).frame(maxWidth: 420)
            }
            if let failure = companion.runFailure {
                RunFailureView(failure: failure, showTitle: true)
            }
            if !companion.shown.isEmpty {
                Reply(
                    text: companion.shown, sources: companion.replySources, companion: companion,
                    finished: !companion.running)
                    .background(alignment: .leading) {
                        if companion.focus == .latest {
                            RoundedRectangle(cornerRadius: Paper.radiusMD).fill(Paper.accentFill)
                        }
                    }
                    .id("latest-reply")
            }
            ForEach(companion.pendingMessages) { item in
                TurnView(
                    turn: Turn(fromOwner: true, text: item.text),
                    companion: companion,
                    focused: false
                )
                .opacity(0.75)
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

    /// One field for everything: a grounded two-row composer with generous input space on top
    /// and a quiet utility strip below.
    private var field: some View {
        let hasText = !companion.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let canSend = hasText || (companion.inputPurpose == .ask && !companion.draftAttachments.isEmpty)
        let canCancel = companion.replyingTo != nil || companion.inputPurpose != .ask
        let dropTargeted = droppingAttachments.on && companion.canAddAttachments
        let composerShape = RoundedRectangle(cornerRadius: Paper.radiusComposer, style: .continuous)

        return VStack(alignment: .leading, spacing: 0) {
            if let quote = companion.replyingTo {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "arrowshape.turn.up.left")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Paper.inkSecondary)
                        .padding(.top, 2)
                    Text(quote)
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkSecondary)
                        .lineLimit(2)
                        .truncationMode(.tail)
                    Spacer(minLength: 4)
                    KeybindChip("Esc")
                    Button {
                        companion.replyingTo = nil
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 9, weight: .semibold))
                            .frame(width: 16, height: 16)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(IconButton(size: 18))
                    .accessibilityLabel("Don't reply to this")
                }
                .padding(.horizontal, 14)
                .padding(.top, 10)
                .padding(.bottom, 8)
                Rectangle().fill(Paper.hairline).frame(height: 0.5)
            }

            if companion.inputPurpose == .ask, !companion.draftAttachments.isEmpty {
                DraftAttachmentChips(files: companion.draftAttachments, remove: companion.removeAttachment)
                    .padding(.horizontal, 14)
                    .padding(.top, 8)
            }

            // Top row: multi-line text input with generous breathing room.
            ZStack(alignment: .topLeading) {
                if companion.draft.isEmpty {
                    Text(companion.inputPurpose.prompt)
                        .font(Paper.Typography.body)
                        .foregroundStyle(Paper.placeholder)
                        .lineLimit(1)
                        .allowsHitTesting(false)
                }
                ChatInput(title: "", text: $companion.draft, focus: $typing, submit: { companion.send() })
                    .font(Paper.Typography.body)
                    .lineLimit(1...6)
                    .onExitCommand {
                        companion.inputPurpose = .ask
                        companion.replyingTo = nil
                    }
            }
            .padding(.horizontal, 14)
            .padding(.top, 12)
            .padding(.bottom, 6)

            // Bottom utility strip: minimal by default, with send/stop action on the right.
            HStack(alignment: .center, spacing: 8) {
                if companion.inputPurpose == .ask {
                    Button { window.chooseAttachments(companion.addAttachments) } label: {
                        Image(systemName: "paperclip")
                            .font(.system(size: 13, weight: .medium))
                    }
                    .buttonStyle(IconButton(size: 28))
                    .help("Attach files")
                    .accessibilityLabel("Attach files")
                    .disabled(!companion.canAddAttachments)
                    if companion.readingAttachments || companion.uploadingAttachments {
                        ProgressView().controlSize(.small)
                            .accessibilityLabel(companion.readingAttachments ? "Reading files" : "Uploading files")
                    }
                }
                if canCancel && companion.replyingTo == nil {
                    Text("Esc to cancel")
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkMuted)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                if companion.needsOnboarding, companion.inputPurpose == .ask {
                    Button("Add a model…") { OnboardingWindow.shared.show() }
                        .buttonStyle(CardButton(kind: .secondary))
                        .padding(.trailing, 4)
                } else if companion.running, !canSend {
                    RoundButton(symbol: "stop.fill", label: "Stop bud", size: 28, action: companion.stop)
                } else {
                    RoundButton(symbol: "arrow.up", label: "Send", size: 28, action: { companion.send() })
                        .opacity(canSend ? 1 : 0.35)
                }
            }
            .padding(.leading, 14)
            .padding(.trailing, 8)
            .padding(.bottom, 8)
            .padding(.top, 2)
        }
        .surface(
            composerShape,
            lift: 1,
            fill: Paper.elevated,
            border: dropTargeted ? Paper.accent : typing ? Paper.borderStrong : Paper.borderSubtle,
            borderWidth: dropTargeted ? 1.5 : typing ? 1 : 0.5
        )
        .animation(.easeOut(duration: 0.15), value: typing)
        .animation(.easeOut(duration: 0.15), value: companion.replyingTo)
        .frame(maxWidth: column)
        .padding(.horizontal, 24)
        .padding(.bottom, 18)
        .padding(.top, 6)
    }

    private var windowControls: some View {
        HStack(spacing: 2) {
            Button(action: window.togglePin) {
                Image(systemName: window.pinned ? "pin.fill" : "pin")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(window.pinned ? Paper.inkPrimary : Paper.inkSecondary)
            }
            .buttonStyle(IconButton(size: 28))
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

/// One message. Yours sits to the right on recessed paper with a 0.5pt hairline; bud's is plain
/// text at full width, the way a page reads, since it's what you came for.
private struct TurnView: View {
    let turn: Turn
    let companion: Companion
    /// A source link pointed here; the message is marked until the moment passes.
    var focused = false
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        content
            .background(
                RoundedRectangle(cornerRadius: Paper.radiusMD, style: .continuous)
                    .fill(focused ? Paper.accentFill : .clear)
                    .padding(-8)
            )
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
                            Text(quote)
                                .font(Paper.Typography.callout)
                                .foregroundStyle(Paper.inkSecondary)
                                .lineLimit(3)
                        }
                        .fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(turn.attachments) { file in
                        SentAttachment(
                            file: file, selection: companion.selection(for: file)
                        ) { companion.openAttachment(file) }
                    }
                    if !parts.text.isEmpty {
                        Text(parts.text)
                            .font(Paper.Typography.body)
                            .lineSpacing(3.5)
                            .textSelection(.enabled)
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
                    Button {
                        companion.openDesktopDelivery(jobID)
                    } label: {
                        Label(turn.text, systemImage: "macwindow")
                            .font(Paper.Typography.callout)
                            .padding(10)
                            .background(Paper.elevated, in: RoundedRectangle(cornerRadius: Paper.radiusMD))
                    }
                    .buttonStyle(.plain)
                    .help("Open browser delivery")
                } else if !turn.text.isEmpty {
                    Reply(text: turn.text, sources: turn.sources, companion: companion, finished: true)
                }
            }
        }
    }
}

/// bud's words, with copy and reply under them when you hover: the actions are there when you
/// reach for them and out of the way when you read.
private struct Reply: View {
    let text: String
    /// What the reply's `[S1]` tags cite; they show as dates that open the cited message.
    var sources: [String: Source] = [:]
    let companion: Companion
    /// A reply still streaming has no actions yet.
    let finished: Bool
    @StateObject private var hovered = Flag()
    @StateObject private var copied = Flag()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            MarkdownView(Citations.linked(text, sources: sources), rich: finished)
            if finished {
                HStack(spacing: 2) {
                    action(copied.on ? "checkmark" : "doc.on.doc", label: copied.on ? "Copied" : "Copy") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(
                            Citations.plain(text, sources: sources), forType: .string)
                        copied.on = true
                        Task {
                            try? await Task.sleep(for: .seconds(1.5))
                            copied.on = false
                        }
                    }
                    action("arrowshape.turn.up.left", label: "Reply") {
                        // The words you highlighted, if any; otherwise the whole reply.
                        companion.replyingTo = Selection.copied() ?? Citations.plain(text, sources: sources)
                    }
                }
                .padding(.leading, -5)
                .opacity(hovered.on ? 1 : 0)
                .animation(.easeOut(duration: 0.15), value: hovered.on)
            }
        }
        .contentShape(Rectangle())
        .onHover { hovered.on = $0 }
    }

    private func action(_ symbol: String, label: String, run: @escaping () -> Void) -> some View {
        Button(action: run) {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .medium))
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(IconButton(size: 24))
        .help(label)
        .accessibilityLabel(label)
    }
}

/// The text highlighted in the window, read by asking it to copy, with the clipboard put back
/// as it was. SwiftUI doesn't expose a text view's selection, but the Copy command reaches it.
@MainActor
private enum Selection {
    static func copied() -> String? {
        let board = NSPasteboard.general
        let saved = board.pasteboardItems?.map { item in
            item.types.compactMap { type in item.data(forType: type).map { (type, $0) } }
        } ?? []
        let before = board.changeCount
        NSApp.sendAction(#selector(NSText.copy(_:)), to: nil, from: nil)
        guard board.changeCount != before else { return nil }
        let text = board.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines)
        board.clearContents()
        board.writeObjects(saved.map { entries in
            let item = NSPasteboardItem()
            for (type, data) in entries { item.setData(data, forType: type) }
            return item
        })
        return text?.isEmpty == false ? text : nil
    }
}

/// While bud works: how long so far, ticking each second, and its steps being ticked off.
private struct Working: View {
    let since: Date
    let steps: [Step]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TimelineView(.periodic(from: since, by: 1)) { context in
                let stepSuffix = steps.isEmpty ? "" : " · \(steps.count) \(steps.count == 1 ? "step" : "steps")"
                Text("Working for \(elapsed(Int(context.date.timeIntervalSince(since))))\(stepSuffix)")
                    .font(Fonts.sans(size: 11.5, tabularDigits: true))
                    .foregroundStyle(Paper.inkSecondary)
                    .shimmer(active: true)
            }
            if !steps.isEmpty {
                StepList(steps: steps, live: true)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .animation(.spring(duration: 0.25, bounce: 0), value: steps.count)
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
            HStack(spacing: 5) {
                Image(systemName: "arrow.down")
                    .font(.system(size: 10, weight: .semibold))
                Text("Jump to latest")
                    .font(Paper.Typography.captionMedium)
            }
            .foregroundStyle(hovered.on ? Paper.inkPrimary : Paper.inkSecondary)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
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
        .help("Jump to the latest")
        .accessibilityLabel("Jump to the latest message")
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

    private var targetSeq: Int? {
        if case .turn(_, let seq) = panel.target { return seq }
        return nil
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
                                SourceTurn(turn: turn, cited: cited.contains(turn.id))
                                    .id(turn.id)
                            }
                        }
                        .padding(16)
                    }
                    .onAppear {
                        if let seq = targetSeq,
                           let anchor = panel.turns.first(where: { $0.seq == seq }) {
                            proxy.scrollTo(anchor.id, anchor: .center)
                        }
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

    private var cited: Set<UUID> {
        guard let turn = panel.citedTurn else { return [] }
        return [turn.id]
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

/// A cited thread in the main chat area. The live conversation stays mounted underneath.
private struct CitedConversationView: View {
    let panel: SourcePanel
    let companion: Companion

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button(action: companion.backFromCitation) {
                    Label("Back", systemImage: "chevron.left")
                }
                .buttonStyle(.plain)
                .keyboardShortcut(.escape, modifiers: [])
                Spacer()
                Text(panel.date.isEmpty ? "Cited conversation" : panel.date)
                    .font(Paper.Typography.callout)
                Spacer()
            }
            .padding(.leading, 84)
            .padding(.trailing, 20)
            .frame(height: Rhythm.headerHeight)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 20) {
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
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .background {
                                if turn.id == panel.citedTurn?.id {
                                    RoundedRectangle(cornerRadius: Paper.radiusMD)
                                        .fill(Paper.accentFill)
                                }
                            }
                            .id(turn.id)
                        }
                    }
                    .frame(maxWidth: 640)
                    .padding(32)
                    .frame(maxWidth: .infinity)
                }
                .task {
                    guard let cited = panel.citedTurn else { return }
                    await Task.yield()
                    proxy.scrollTo(cited.id, anchor: .center)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Paper.canvas)
    }
}
