import AppKit
import SwiftUI

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

    func togglePin() {
        pinned.toggle()
        UserDefaults.standard.set(pinned, forKey: "windowPinned")
        window?.level = pinned ? .floating : .normal
    }

    func windowWillClose(_ notification: Notification) {
        let companion = Companion.shared
        companion.windowOpen = false
        // Back to the bubble, with whatever bud last said or still needs.
        companion.bubbleOpen = !companion.shown.isEmpty || !companion.approvals.isEmpty
    }

    private func make() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 640),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "bud"
        // The paper runs to the top edge; the traffic lights sit on it.
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(Paper.surface)
        window.minSize = NSSize(width: 380, height: 420)
        window.level = pinned ? .floating : .normal
        window.delegate = self
        window.contentView = NSHostingView(rootView: ConversationView(companion: Companion.shared, window: self))
        if !window.setFrameUsingName("BudConversation") { window.center() }
        window.setFrameAutosaveName("BudConversation")
        return window
    }
}

/// Replies in the window read like a page: Georgia, a size up from the bubble, with air
/// between lines. (Claude's own serif isn't on the Mac; Georgia is, and is built for screens.)
private let pageType = ReplyType(body: .custom("Georgia", size: 15), lineSpacing: 4)

/// The rhythm that separates speakers without labels: a wide gap before each of your
/// messages starts a new exchange, a shorter one joins bud's answer to your question, about
/// two to three as in claude.ai. The exchange gap includes the row of hover actions under a
/// reply, which is always laid out, so the gap reads the same whether or not they show.
private enum Rhythm {
    static let exchange: CGFloat = 52
    static let answer: CGFloat = 22
    /// The actions row under a reply: its spacing and height.
    static let actions: CGFloat = 6 + 24
}

private final class ScrollState: ObservableObject {
    @Published var atBottom = true
}

private struct ConversationView: View {
    @Bindable var companion: Companion
    let window: ConversationWindow
    @FocusState private var typing: Bool
    @StateObject private var scroll = ScrollState()

    /// Lines longer than this are hard to read, however wide the window gets.
    private let column: CGFloat = 640

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(companion.turns.enumerated()), id: \.element.id) { index, turn in
                            TurnView(turn: turn, companion: companion)
                                .padding(.top, index == 0 ? 0 : turn.fromOwner ? Rhythm.exchange - Rhythm.actions : Rhythm.answer)
                        }
                        latest
                            .padding(.top, companion.turns.isEmpty ? 0 : Rhythm.answer)
                        Color.clear.frame(height: 1).id("end")
                    }
                    .frame(maxWidth: column, alignment: .leading)
                    .padding(.horizontal, 32)
                    .padding(.top, 52)
                    .padding(.bottom, 24)
                    .frame(maxWidth: .infinity)
                }
                // Opens at the latest message, and stays there as a reply streams in, unless
                // you've scrolled up to read something earlier.
                .defaultScrollAnchor(.bottom)
                .defaultScrollAnchor(.bottom, for: .sizeChanges)
                .onScrollGeometryChange(for: Bool.self) { geometry in
                    // Measured by what's on screen, which already accounts for the scroll insets.
                    geometry.visibleRect.maxY >= geometry.contentSize.height - 24
                } action: { _, atBottom in
                    scroll.atBottom = atBottom
                }
                // Text slides under the title bar softly rather than meeting a hard edge.
                .mask {
                    VStack(spacing: 0) {
                        LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom)
                            .frame(height: 44)
                        Color.black
                    }
                }
                .overlay(alignment: .bottom) {
                    if !scroll.atBottom {
                        JumpButton {
                            withAnimation(.spring(duration: 0.35, bounce: 0)) {
                                proxy.scrollTo("end", anchor: .bottom)
                            }
                        }
                        .padding(.bottom, 10)
                        .transition(.opacity.combined(with: .scale(scale: 0.9)))
                    }
                }
                .animation(.easeOut(duration: 0.15), value: scroll.atBottom)
                .onAppear {
                    // Replies finish laying out a moment after the first pass, which leaves the
                    // anchor short of the end; settle on the latest message once they have.
                    Task {
                        try? await Task.sleep(for: .milliseconds(120))
                        proxy.scrollTo("end", anchor: .bottom)
                    }
                }
            }
            field
        }
        .foregroundStyle(Paper.ink)
        .background(Paper.surface)
        .environment(\.replyType, pageType)
        .overlay(alignment: .topTrailing) { pin }
        .onAppear { typing = true }
        .onChange(of: companion.inputPurpose) { _, purpose in
            if purpose != .ask { typing = true }
        }
        .onChange(of: companion.replyingTo) { _, quote in
            if quote != nil { typing = true }
        }
    }

    /// The reply in progress, or the last one, with what bud is doing and anything it needs.
    @ViewBuilder private var latest: some View {
        VStack(alignment: .leading, spacing: 12) {
            if companion.status == .working, let start = companion.runStart {
                Working(since: start, steps: companion.steps)
            } else if let work = companion.lastWork, !companion.shown.isEmpty {
                WorkLine(work: work)
            }
            ForEach(companion.notices, id: \.self) { notice in
                Label(notice, systemImage: "arrow.turn.down.right")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            if companion.unsentSpeech != nil {
                UnsentSpeech(companion: companion).frame(maxWidth: 420)
            }
            if !companion.shown.isEmpty {
                Reply(text: companion.shown, companion: companion, finished: companion.status != .working)
            }
            if !companion.approvals.isEmpty {
                ApprovalDeck(companion: companion)
                    .frame(maxWidth: 420, alignment: .leading)
                    .padding(14)
                    .surface(RoundedRectangle(cornerRadius: 14, style: .continuous))
            } else if let receipt = companion.receipt {
                ReceiptLine(receipt: receipt)
            }
        }
    }

    /// One field for everything: a new message, a reply to something bud said, or your answer
    /// when bud asked for one.
    private var field: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let quote = companion.replyingTo {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "arrowshape.turn.up.left")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .padding(.top, 2)
                    Text(quote)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .truncationMode(.tail)
                    Spacer(minLength: 4)
                    Button {
                        companion.replyingTo = nil
                    } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 16, height: 16)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Don't reply to this")
                }
                .transition(.opacity)
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField(companion.inputPurpose.prompt, text: $companion.draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.body)
                    .lineLimit(1...6)
                    .focused($typing)
                    .onSubmit { companion.send() }
                    .onExitCommand {
                        companion.inputPurpose = .ask
                        companion.replyingTo = nil
                    }
                    .padding(.vertical, 4)
                if companion.status == .working {
                    RoundButton(symbol: "stop.fill", label: "Stop bud", action: companion.stop)
                } else {
                    RoundButton(symbol: "arrow.up", label: "Send", action: { companion.send() })
                        .opacity(companion.draft.trimmingCharacters(in: .whitespaces).isEmpty ? 0.35 : 1)
                }
            }
        }
        .padding(.leading, 16)
        .padding(.trailing, 8)
        .padding(.vertical, 8)
        .surface(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .animation(.easeOut(duration: 0.15), value: companion.replyingTo)
        .frame(maxWidth: column)
        .padding(.horizontal, 24)
        .padding(.bottom, 18)
        .padding(.top, 6)
    }

    /// Top right, where Siri keeps its own: back to the bubble, and keep on top.
    private var pin: some View {
        HStack(spacing: 2) {
            Button(action: window.togglePin) {
                Image(systemName: window.pinned ? "pin.fill" : "pin")
                    .font(.system(size: 11, weight: .medium))
            }
            .buttonStyle(IconButton(size: 28))
            .help(window.pinned ? "Stop keeping on top" : "Keep on top of other windows")
            .accessibilityLabel(window.pinned ? "Unpin window" : "Keep window on top")
            Button(action: window.collapse) {
                Image(systemName: "arrow.down.right.and.arrow.up.left")
                    .font(.system(size: 11, weight: .medium))
            }
            .buttonStyle(IconButton(size: 28))
            .help("Back to the bubble")
            .accessibilityLabel("Collapse to the bubble")
        }
        .padding(.top, 6)
        .padding(.trailing, 10)
    }
}

/// One message. Yours sits to the right in a filled box, with any reply quote inside it;
/// bud's is plain text at full width, the way a page reads, since it's what you came for.
private struct TurnView: View {
    let turn: Turn
    let companion: Companion

    var body: some View {
        if turn.fromOwner {
            let parts = Companion.unquoted(turn.text)
            HStack {
                Spacer(minLength: 80)
                VStack(alignment: .leading, spacing: 6) {
                    if let quote = parts.quote {
                        HStack(spacing: 8) {
                            RoundedRectangle(cornerRadius: 1).fill(Paper.ink.opacity(0.2)).frame(width: 2)
                            Text(quote)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .lineLimit(3)
                        }
                        .fixedSize(horizontal: false, vertical: true)
                    }
                    Text(parts.text)
                        .font(.system(size: 14))
                        .lineSpacing(3)
                        .textSelection(.enabled)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Paper.ink.opacity(0.065), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            }
        } else {
            VStack(alignment: .leading, spacing: 12) {
                if let work = turn.work { WorkLine(work: work) }
                Reply(text: turn.text, companion: companion, finished: true)
            }
        }
    }
}

/// bud's words, with copy and reply under them when you hover, as T3 Code does: the actions
/// are there when you reach for them and out of the way when you read.
private struct Reply: View {
    let text: String
    let companion: Companion
    /// A reply still streaming has no actions yet.
    let finished: Bool
    @StateObject private var hovered = Flag()
    @StateObject private var copied = Flag()

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            MarkdownView(text)
            if finished {
                HStack(spacing: 2) {
                    action(copied.on ? "checkmark" : "doc.on.doc", label: copied.on ? "Copied" : "Copy") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(text, forType: .string)
                        copied.on = true
                        Task {
                            try? await Task.sleep(for: .seconds(1.5))
                            copied.on = false
                        }
                    }
                    action("arrowshape.turn.up.left", label: "Reply") {
                        // The words you highlighted, if any; otherwise the whole reply.
                        companion.replyingTo = Selection.copied() ?? text
                    }
                }
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
                Text("Working for \(elapsed(Int(context.date.timeIntervalSince(since))))")
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .shimmer(active: true)
            }
            if !steps.isEmpty {
                StepList(steps: steps, live: true)
            }
        }
    }
}

/// A finished run folded to one muted line, "Worked for 34s · 4 steps ›", that opens the steps.
private struct WorkLine: View {
    let work: Work
    @StateObject private var open = Flag()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if work.steps.isEmpty {
                summary
            } else {
                Button { open.on.toggle() } label: {
                    HStack(spacing: 4) {
                        summary
                        Image(systemName: "chevron.right")
                            .font(.system(size: 8, weight: .bold))
                            .foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(open.on ? 90 : 0))
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(open.on ? "Hide bud's steps" : "Show bud's steps")
                if open.on {
                    StepList(steps: work.steps, live: false, limit: nil)
                        .transition(.opacity)
                }
            }
        }
        .animation(.spring(duration: 0.25, bounce: 0), value: open.on)
    }

    private var summary: some View {
        let count = work.steps.count
        let steps = count == 0 ? "" : " · \(count) \(count == 1 ? "step" : "steps")"
        return Text("Worked for \(elapsed(work.seconds))\(steps)")
            .font(.callout.monospacedDigit())
            .foregroundStyle(.secondary)
    }
}

/// "8s", "1m 05s", "1h 02m": the same shape T3 Code uses, so a ticking timer doesn't jitter.
private func elapsed(_ seconds: Int) -> String {
    let seconds = max(0, seconds)
    if seconds < 60 { return "\(seconds)s" }
    let minutes = seconds / 60
    if minutes < 60 { return "\(minutes)m \(String(format: "%02d", seconds % 60))s" }
    return "\(minutes / 60)h \(String(format: "%02d", minutes % 60))m"
}

/// Back to the newest message, when you've scrolled up to read.
private struct JumpButton: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: "arrow.down")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Paper.ink)
                .frame(width: 30, height: 30)
                .surface(Circle(), lift: 0.6)
        }
        .buttonStyle(IconButton(size: 30, highlight: false))
        .help("Jump to the latest")
        .accessibilityLabel("Jump to the latest message")
    }
}

private struct RoundButton: View {
    let symbol: String
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 11, weight: .bold))
                .frame(width: 26, height: 26)
                .background(Paper.ink, in: Circle())
        }
        .buttonStyle(IconButton(size: 26, highlight: false))
        .help(label)
        .accessibilityLabel(label)
    }
}
