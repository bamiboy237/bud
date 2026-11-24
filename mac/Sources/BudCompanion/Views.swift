import SwiftUI

// Motion: one spring for things that appear, a quicker fade for things that leave.
// Critically damped (no bounce): nothing here was thrown, so nothing should overshoot.
private let appear = Animation.spring(duration: 0.3, bounce: 0)
private let leave = Animation.easeOut(duration: 0.15)

/// Everything in the floating panel, anchored to the pet in the bottom-right corner.
struct PetView: View {
    @Bindable var companion: Companion
    var focusPanel: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var anchor: Anchor { companion.anchor }

    var body: some View {
        // The bubble and the field stack away from the pet: up from it, or down from it near
        // the top of the screen. The pet keeps its corner; everything else lines up with it.
        VStack(alignment: anchor.left ? .leading : .trailing, spacing: 8) {
            if anchor.below {
                petRow
                if companion.inputOpen && !companion.windowOpen { InputField(companion: companion) }
                bubble
                Spacer(minLength: 0)
            } else {
                Spacer(minLength: 0)
                bubble
                if companion.inputOpen && !companion.windowOpen { InputField(companion: companion) }
                petRow
            }
        }
        .padding(PetPanel.inset)
        .frame(width: PetPanel.size.width, height: PetPanel.size.height, alignment: corner)
        .animation(companion.bubbleOpen ? appear : leave, value: companion.bubbleOpen)
        .animation(leave, value: companion.status)
    }

    private var corner: Alignment {
        switch (anchor.below, anchor.left) {
        case (false, false): .bottomTrailing
        case (false, true): .bottomLeading
        case (true, false): .topTrailing
        case (true, true): .topLeading
        }
    }

    @ViewBuilder private var bubble: some View {
        if companion.bubbleOpen && !companion.windowOpen {
            Bubble(companion: companion)
                .transition(bubbleTransition)
        }
    }

    /// The stop button sits on the bubble's side of the pet.
    private var petRow: some View {
        HStack(alignment: .bottom, spacing: 8) {
            if anchor.left {
                pet
                pupStack
            }
            if Voice.shared.state == .listening || Voice.shared.state == .transcribing {
                VoicePill(voice: Voice.shared)
                    .transition(.opacity.combined(with: .scale(scale: 0.9, anchor: anchor.left ? .leading : .trailing)))
            } else if companion.running {
                StopButton(action: companion.stop)
                    .transition(.opacity)
            }
            if !anchor.left {
                pupStack
                pet
            }
        }
        .animation(.spring(duration: 0.25, bounce: 0), value: Voice.shared.state)
        .animation(.spring(duration: 0.35, bounce: 0), value: companion.activeTasks.isEmpty)
    }

    @ViewBuilder private var pupStack: some View {
        if !companion.visiblePups.isEmpty {
            let overflow = max(0, companion.activeTasks.count - companion.visiblePups.count)
            HStack(alignment: .bottom, spacing: 4) {
                ForEach(companion.visiblePups) { task in
                    ChildPupView(task: task)
                        .transition(.scale(scale: 0.6).combined(with: .opacity))
                }
                if overflow > 0 {
                    Text("+\(overflow)")
                        .font(Paper.Typography.ledgerMeta)
                        .foregroundStyle(Paper.inkSecondary)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(.ultraThinMaterial, in: Capsule())
                        .help("\(overflow) more background job\(overflow == 1 ? "" : "s") running")
                        .transition(.opacity)
                }
            }
            .animation(.spring(duration: 0.35, bounce: 0), value: companion.activeTasks)
        }
    }

    private var pet: some View {
                PetButton(companion: companion) {
                    // The release that ends a drag isn't a click.
                    if companion.dragging { return }
                    // With the window open, bud talks there: the pet brings it forward.
                    if companion.windowOpen {
                        ConversationWindow.shared.show()
                        return
                    }
                    // Something waiting comes first: a click shows it rather than a blank field.
                    if companion.needsAttention && !companion.bubbleOpen {
                        companion.bubbleOpen = true
                        return
                    }
                    companion.inputOpen.toggle()
                    if !companion.inputOpen { companion.inputPurpose = .ask }
                    if companion.inputOpen { focusPanel() }
                }
    }

    /// Grows out of the pet's corner, so it reads as the pet speaking. Reduced motion: fade only.
    /// Grows out of the pet and shrinks back into it: in and out along the same path.
    private var bubbleTransition: AnyTransition {
        let toward = UnitPoint(x: anchor.left ? 0 : 1, y: anchor.below ? 0 : 1)
        return reduceMotion
            ? .opacity
            : .asymmetric(
                insertion: .scale(scale: 0.96, anchor: toward).combined(with: .opacity),
                removal: .scale(scale: 0.98, anchor: toward).combined(with: .opacity))
    }
}

// MARK: Pet

private struct PetButton: View {
    let companion: Companion
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            if let sprite = PetSprite.named(companion.petID) {
                pet(sprite)
            } else {
                paw
            }
        }
        .buttonStyle(PressScale())
        // Drag the pet to move the whole panel; a few points of slack keep clicks clicks.
        .simultaneousGesture(
            DragGesture(minimumDistance: 4, coordinateSpace: .global)
                .onChanged { _ in
                    companion.dragging = true
                    companion.carry(pointerX: NSEvent.mouseLocation.x)
                    PetPanel.current?.drag(ended: false)
                }
                .onEnded { _ in
                    PetPanel.current?.drag(ended: true)
                    companion.setDown()
                    // Cleared just after the button has seen the release.
                    Task {
                        try? await Task.sleep(for: .milliseconds(80))
                        companion.dragging = false
                    }
                })
        .accessibilityLabel("bud")
        .accessibilityValue(statusText)
        .accessibilityHint("Ask bud something")
        .help(statusText)
        .contextMenu {
            Button(companion.bubbleOpen ? "Hide reply" : "Show reply") {
                companion.bubbleOpen.toggle()
            }
        }
    }

    private var connected: Bool { companion.link == .connected }

    /// The pet acts out the status; colour appears only when bud needs you.
    private func pet(_ sprite: PetSprite) -> some View {
        AnimatedPet(sprite: sprite, pose: { companion.pose })
            // Away from bud: greyed and faded, so a stale pet never looks like a working one.
            .saturation(connected ? 1 : 0)
            .opacity(connected ? 1 : 0.55)
            .overlay(alignment: .topTrailing) {
                ZStack {
                    if companion.status == .needsYou && connected {
                        WaitingBadge(count: companion.waitingCount)
                            .fixedSize()
                            .transition(.opacity)
                    }
                }
                // A fixed spot in the pet's corner. A wider "9+" badge overflows to the left.
                .frame(width: 16, height: 16, alignment: .trailing)
                .padding(10)
                .allowsHitTesting(false)
            }
            .shadow(color: .black.opacity(0.22), radius: 5, y: 3)
            .contentShape(Rectangle())
            .onHover { if $0 { companion.greet() } }
            .animation(.easeOut(duration: 0.2), value: connected)
    }

    /// Fallback when no pet art is installed.
    private var paw: some View {
            Image(systemName: "pawprint.fill")
                .font(.system(size: 24, weight: .medium))
                .foregroundStyle(.primary)
                .frame(width: 56, height: 56)
                .surface(Circle(), lift: 0.6)
                .overlay(StatusRing(status: companion.status, link: companion.link))
                .contentShape(Circle())
    }

    private var statusText: String {
        if case .problem(let detail) = companion.link { return detail }
        return companion.status.label
    }
}

// MARK: Mini Kid Pup

/// A mini kid/pup sub-agent sprite representing a background job working beside Bud.
struct ChildPupView: View {
    static let defaultSize = CGSize(width: 44, height: 48)

    let move: PetMove
    let title: String
    let statusText: String
    var size: CGSize = defaultSize
    var since: Date = .now
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(task: RunningTask, size: CGSize = defaultSize) {
        self.move = task.move
        self.title = task.title
        self.statusText = task.status
        self.size = size
        self.since = task.finishedAt ?? .now
    }

    var body: some View {
        Button {
            ConversationWindow.shared.show()
        } label: {
            if let sprite = PetSprite.named("pup") ?? PetSprite.named("dog") {
                pupImage(sprite)
            } else {
                fallbackPaw
            }
        }
        .buttonStyle(PressScale())
        .pointerStyle(.link)
        .help("\(title) (\(statusText))")
        .accessibilityLabel("Helper pup for \(title)")
        .accessibilityValue(move == .working ? "Working" : "Done")
    }

    private func pupImage(_ sprite: PetSprite) -> some View {
        TimelineView(.animation(minimumInterval: 0.08, paused: reduceMotion)) { context in
            let moment = reduceMotion ? Date.distantPast : context.date
            let pose = PetPose(status: move, statusSince: reduceMotion ? Date.distantPast : since)
            if let rect = sprite.frame(for: pose, at: moment),
               let image = sprite.image(rect) {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .scaledToFit()
            }
        }
        .frame(width: size.width, height: size.height)
        .shadow(color: .black.opacity(0.18), radius: 3, y: 1.5)
    }

    private var fallbackPaw: some View {
        Image(systemName: "pawprint.fill")
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(Paper.inkSecondary)
            .frame(width: size.width, height: size.height)
    }
}

/// "Needs you", Option A: an inverted ink pill with how many things wait, cut out of the pet by
/// a paper ring. Pure ink, so it never collides with links, the golden dog, or errors.
struct WaitingBadge: View {
    let count: Int

    var body: some View {
        Text(count > 9 ? "9+" : "\(max(count, 1))")
            .font(Fonts.mono(size: 10, bold: true, tabularDigits: true))
            .foregroundStyle(Paper.canvas)
            .padding(.horizontal, count > 9 ? 4 : 0)
            .frame(minWidth: 16, minHeight: 16)
            .background(Capsule().fill(Paper.inkPrimary))
            .overlay(Capsule().strokeBorder(Paper.canvas, lineWidth: 1.5))
            .accessibilityLabel("\(count) waiting for you")
    }
}

/// Feedback on press-down, not release: the pet answers your click at once.
private struct PressScale: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
            .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
    }
}

/// The pet's status, around its edge. Colour only where you need to act.
private struct StatusRing: View {
    let status: PetStatus
    let link: Link
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            if case .problem = link {
                Circle().strokeBorder(Paper.inkSecondary, style: StrokeStyle(lineWidth: 2, dash: [3, 4]))
            } else {
                switch status {
                case .working:
                    working
                case .needsYou:
                    Circle().strokeBorder(Paper.inkPrimary, lineWidth: 2)
                    Circle().fill(Paper.inkPrimary).frame(width: 12, height: 12)
                        .overlay(Circle().strokeBorder(Paper.canvas, lineWidth: 1.5))
                        .offset(x: 20, y: -20)
                case .failed:
                    Circle().strokeBorder(Paper.errorInk, lineWidth: 2)
                case .idle, .done:
                    EmptyView()
                }
            }
        }
        .allowsHitTesting(false)
    }

    /// A spinner is constant motion, so it turns linearly, driven by the display clock.
    /// Reduced motion: a still, faint ring.
    @ViewBuilder private var working: some View {
        if reduceMotion {
            Circle().strokeBorder(Paper.inkSecondary.opacity(0.6), lineWidth: 2)
        } else {
            TimelineView(.animation) { context in
                let turn = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 0.9) / 0.9
                Circle()
                    .trim(from: 0, to: 0.28)
                    .stroke(Paper.inkSecondary, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .padding(1)
                    .rotationEffect(.degrees(turn * 360))
            }
        }
    }
}

private struct StopButton: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: "stop.fill")
                .font(.system(size: 10, weight: .semibold))
                .frame(width: 28, height: 28)
                .surface(Circle(), lift: 0.6)
                .contentShape(Circle())
        }
        .buttonStyle(PressScale())
        .pointerStyle(.link)
        .help("Stop")
        .accessibilityLabel("Stop bud")
    }
}

/// Beside the pet while you talk: bars that follow your voice, so you can see it's hearing
/// you. While the words are being worked out, a quiet shimmer instead.
private struct VoicePill: View {
    let voice: Voice
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// Each bar takes a share of the level, the middle ones most, so it reads as a voice.
    private let shape: [Float] = [0.45, 0.8, 1, 0.7, 0.4]

    var body: some View {
        HStack(spacing: 3) {
            if voice.state == .listening {
                ForEach(shape.indices, id: \.self) { index in
                    Capsule()
                        .fill(Paper.inkPrimary.opacity(0.75))
                        .frame(width: 3, height: 4 + CGFloat(voice.level * shape[index]) * 14)
                }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.08), value: voice.level)
            } else {
                Text("Writing")
                    .font(Paper.Typography.captionMedium)
                    .foregroundStyle(Paper.inkSecondary)
                    .shimmer(active: true)
            }
        }
        .frame(height: 20)
        .padding(.horizontal, 12)
        .padding(.vertical, 5)
        .surface(Capsule(), lift: 0.6)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(voice.state == .listening ? "Listening. Let go to send, Esc to cancel." : "Writing down what you said")
    }
}

/// Words you said that didn't go out, with the choice to send them now or let them go.
struct UnsentSpeech: View {
    let companion: Companion

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("“\(companion.unsentSpeech ?? "")”")
                .font(Paper.Typography.callout)
                .foregroundStyle(Paper.inkSecondary)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(companion.speechDestination == .ask ? "Send" : "Add to draft") { companion.resendSpeech() }
                .buttonStyle(CardButton(kind: .secondary))
            Button { companion.dropSpeech() } label: {
                Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
            }
            .buttonStyle(IconButton(size: 20))
            .accessibilityLabel("Don't send")
        }
    }
}

struct UnsentMessage: View {
    let companion: Companion

    var body: some View {
        if let message = companion.heldMessages.first {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("“\(message.text)”")
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button("Retry") { companion.retryHeldMessage() }
                    .buttonStyle(CardButton(kind: .secondary))
                Button { companion.dropHeldMessage() } label: {
                    Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
                }
                .buttonStyle(IconButton(size: 20))
                .accessibilityLabel("Discard unsent message")
            }
            .disabled(companion.retryingHeldMessage)
        }
    }
}

// MARK: Bubble

private struct Bubble: View {
    @Bindable var companion: Companion

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if !compact, !companion.question.isEmpty, companion.setups.isEmpty {
                // What you asked, so the answer keeps its context. One line: a reminder.
                Text(companion.question)
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkMuted)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .padding(.top, -4)
                    .transition(.opacity)
            }
            if companion.running, companion.stepsLayout == .checklist, !companion.steps.isEmpty {
                HStack(alignment: .top, spacing: 10) {
                    RoundedRectangle(cornerRadius: 1)
                        .fill(Paper.accent.opacity(0.4))
                        .frame(width: 2)
                    StepList(steps: companion.steps, live: true)
                }
                .fixedSize(horizontal: false, vertical: true)
            }
            if companion.showStepsAfter, !companion.running, !companion.lastSteps.isEmpty {
                StepsSummary(companion: companion)
            }
            ForEach(companion.notices) { notice in
                VStack(alignment: .leading, spacing: 4) {
                    Label(notice.text, systemImage: "arrow.turn.down.right")
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkSecondary)
                        .lineLimit(2)
                    if let report = notice.report { OpenReport(file: report) }
                }
            }
            if companion.unsentSpeech != nil { UnsentSpeech(companion: companion) }
            if !companion.heldMessages.isEmpty { UnsentMessage(companion: companion) }
            if !companion.shown.isEmpty, companion.setups.isEmpty {
                // The bubble hugs its text up to `maxReply`; longer replies scroll. Reading
                // starts at the top, so streamed text grows below the fold instead of pulling
                // the page away from you. Measured, because a ScrollView takes every point it's
                // offered.
                let overflows = companion.replyHeight > maxReply
                ScrollView {
                    // Measured before the padding, so adding it can't flip `overflows` back.
                    MarkdownView(Citations.linked(companion.shown, sources: companion.replySources))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
                            companion.replyHeight = $0
                        }
                        .padding(.bottom, overflows ? 16 : 0)
                }
                .scrollIndicators(.never)
                .scrollBounceBehavior(.basedOnSize)
                .onScrollGeometryChange(for: ReplyMore.self) { geometry in
                    let top = geometry.contentOffset.y + geometry.contentInsets.top
                    return ReplyMore(
                        above: top > 1,
                        below: top + geometry.containerSize.height < geometry.contentSize.height - 1)
                } action: { _, more in
                    companion.replyMore = more
                }
                .frame(height: min(max(companion.replyHeight, 1), maxReply))
                // A soft edge, not a divider or a scrollbar, says "there's more": on each side
                // that has more, and only while it does.
                .mask(edgeFade(companion.replyMore))
                .animation(.easeOut(duration: 0.15), value: companion.replyMore)
            }
            if !companion.setups.isEmpty {
                SetupDeck(companion: companion)
                    .transition(cardTransition)
            }
            if !companion.approvals.isEmpty {
                ApprovalDeck(companion: companion)
                    .transition(cardTransition)
            } else if let receipt = companion.receipt {
                ReceiptLine(receipt: receipt)
                    .transition(.opacity)
            }
        }
        .foregroundStyle(Paper.inkPrimary)
        .padding(.horizontal, 18)
        // The tail's edge gets its extra room, toward the pet.
        .padding(.top, 14 + (companion.anchor.below ? BubbleShape.tail : 0))
        .padding(.bottom, 16 + (companion.anchor.below ? 0 : BubbleShape.tail))
        // Only the name line: a small tag over the pet, not a wide empty card.
        .frame(width: compact ? nil : 332, alignment: .leading)
        .frame(minWidth: 132, alignment: .leading)
        .fixedSize(horizontal: compact, vertical: false)
        .background(
            DitherBackdrop()
                .clipShape(BubbleShape(anchor: companion.anchor))
        )
        .surface(BubbleShape(anchor: companion.anchor))
        .onHover { companion.bubbleHovered = $0 }
        // A cited date opens the conversation window at that message.
        .environment(\.openURL, OpenURLAction { url in
            companion.openSource(url) ? .handled : .systemAction
        })
        .animation(.easeOut(duration: 0.15), value: companion.approvals.first?.id)
        .animation(.easeOut(duration: 0.2), value: companion.receipt)
    }

    private func edgeFade(_ more: ReplyMore) -> some View {
        LinearGradient(
            stops: [
                .init(color: more.above ? .clear : .black, location: 0),
                .init(color: .black, location: more.above ? 0.14 : 0),
                .init(color: .black, location: more.below ? 0.84 : 1),
                .init(color: more.below ? .clear : .black, location: 1),
            ],
            startPoint: .top, endPoint: .bottom)
    }

    private var compact: Bool {
        companion.shown.isEmpty && companion.notices.isEmpty && !companion.needsAttention
            && companion.receipt == nil && companion.steps.isEmpty && companion.unsentSpeech == nil
            && companion.heldMessages.isEmpty
    }

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// A card rises toward you as it arrives, from the pet's side, and fades on its way out.
    private var cardTransition: AnyTransition {
        reduceMotion
            ? .opacity
            : .asymmetric(
                insertion: .offset(y: companion.anchor.below ? -6 : 6).combined(with: .opacity),
                removal: .opacity)
    }

    /// The reply gives way when a card needs the room, so both fit above the pet.
    private var maxReply: CGFloat {
        companion.approvals.isEmpty && companion.setups.isEmpty ? 260 : 60
    }

    /// Who is speaking and what they're doing, like a name above a message.
    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("bud")
                .font(Paper.Typography.wordmark)
            if let state {
                if companion.status == .needsYou {
                    // Option A: pure ink, never a colour that could pass for a link or an error.
                    Rectangle().fill(Paper.inkPrimary).frame(width: 4.25, height: 4.25)
                        .rotationEffect(.degrees(45))
                        .alignmentGuide(.firstTextBaseline) { $0.height / 2 + 4 }
                        .accessibilityHidden(true)
                }
                Text(state.text)
                    .font(Fonts.sans(
                        size: 12,
                        weight: companion.status == .needsYou
                            ? Fonts.OpticalWeight.medium
                            : Fonts.OpticalWeight.regular))
                    .foregroundStyle(state.tint)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .shimmer(active: state.busy)
                    .contentTransition(.opacity)
                    .id(state.text)
                    .transition(.opacity)
            }
            Spacer(minLength: compact ? 6 : 0)
            if let count = stepCount {
                Text(count)
                    .font(Paper.Typography.ledgerMeta)
                    .foregroundStyle(Paper.inkMuted)
                    .contentTransition(.numericText())
            }
            expand
            close
        }
        .animation(.easeOut(duration: 0.15), value: state?.text)
    }

    /// "step 3", for the counted layout while bud works.
    private var stepCount: String? {
        guard companion.stepsLayout == .counted, companion.running,
              !companion.steps.isEmpty else { return nil }
        return "step \(companion.steps.count)"
    }

    /// Shown only on hover: less chrome on a surface that floats over your work.
    private var close: some View {
        chrome("xmark", label: "Close reply", restOpacity: 0) { companion.bubbleOpen = false }
    }

    /// Opens the whole conversation in a window, for long replies and earlier messages.
    private var expand: some View {
        chrome("arrow.up.left.and.arrow.down.right", label: "Open in a window", restOpacity: 0.7) {
            ConversationWindow.shared.show()
        }
        .help("Open in a window")
    }

    private func chrome(
        _ symbol: String,
        label: String,
        restOpacity: Double,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 9, weight: .semibold))
        }
        .buttonStyle(IconButton(size: 20))
        .opacity(companion.bubbleHovered ? 1 : restOpacity)
        .animation(.easeOut(duration: 0.12), value: companion.bubbleHovered)
        .accessibilityLabel(label)
    }

    /// Plain words for what bud is doing. Colour only when it's your move or something broke.
    private var state: (text: String, tint: Color, busy: Bool)? {
        if case .problem(let detail) = companion.link {
            return (detail, Paper.inkSecondary, false)
        }
        switch companion.status {
        case .working:
            // The live line: the step bud is on, unless the checklist below already shows it.
            if companion.stepsLayout != .checklist, let step = companion.steps.last {
                return (step.text, Paper.inkSecondary, true)
            }
            return ("Working on it", Paper.inkSecondary, true)
        case .needsYou:
            if !companion.setups.isEmpty { return ("Needs you", Paper.inkPrimary, false) }
            if companion.approvals.isEmpty { return ("A job needs you", Paper.inkPrimary, false) }
            let asking = companion.hasQuestion
            return (asking ? "Has a question" : "Needs your OK", Paper.inkPrimary, false)
        case .failed: return ("Couldn't finish", Paper.errorInk, false)
        case .done: return ("Done", Paper.inkSecondary, false)
        case .idle: return nil
        }
    }
}

// MARK: Steps

/// bud's steps as rows: ticked when done, the current one shimmering. Long runs show the
/// latest few, with a count of the earlier ones, so the bubble doesn't grow without end.
struct StepList: View {
    let steps: [Step]
    /// While the run is going, the last unfinished step is the live one.
    let live: Bool
    /// How many rows to show; the rest are counted. Nil shows every step.
    var limit: Int? = 5

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            if let limit, steps.count > limit {
                Text("\(steps.count - limit) earlier \(steps.count - limit == 1 ? "step" : "steps")")
                    .font(Paper.Typography.ledgerMeta)
                    .foregroundStyle(Paper.inkMuted)
                    .padding(.leading, 20)
            }
            ForEach(steps.suffix(limit ?? steps.count)) { step in
                // Every unfinished step is in flight: bud runs a call's functions together.
                StepRow(step: step, live: live && !step.done)
                    .transition(.opacity.combined(with: .offset(y: 4)))
            }
        }
        .animation(.spring(duration: 0.3, bounce: 0), value: steps.map(\.id))
    }
}

/// One step, checked off the moment it finishes: the circle fills, a tick draws itself
/// inside it. Steps from the same call finish together, so each ticks 70 ms after the one
/// above it and they read as a run of ticks, not one blink.
private struct StepRow: View {
    let step: Step
    let live: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// The step's place within its call, from its id ("call:2").
    private var order: Int { Int(step.id.split(separator: ":").last ?? "") ?? 0 }

    private var check: Animation {
        // Reduced motion keeps the state change as a fade; nothing draws or grows.
        reduceMotion ? .easeOut(duration: 0.2) : .spring(duration: 0.28, bounce: 0).delay(Double(min(order, 4)) * 0.07)
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            ZStack {
                Circle()
                    .strokeBorder(Paper.borderStrong, lineWidth: 1)
                    .opacity(step.done ? 0 : 1)
                Circle()
                    .fill(Paper.inkPrimary.opacity(0.85))
                    .scaleEffect(step.done || reduceMotion ? 1 : 0.6)
                    .opacity(step.done ? 1 : 0)
                Tick()
                    .trim(from: 0, to: step.done || reduceMotion ? 1 : 0)
                    .stroke(Paper.canvas, style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
                    .opacity(step.done ? 1 : 0)
                    .padding(3)
            }
            .frame(width: 12, height: 12)
            .alignmentGuide(.firstTextBaseline) { $0[VerticalAlignment.center] + 4 }
            .animation(check, value: step.done)

            Text(step.text)
                .font(Paper.Typography.callout)
                .foregroundStyle(live ? Paper.inkPrimary : Paper.inkSecondary)
                .lineLimit(1)
                .truncationMode(.tail)
                .shimmer(active: live)
        }
        .accessibilityElement(children: .combine)
        .accessibilityValue(step.done ? "done" : live ? "in progress" : "")
    }
}

/// A tick, drawn from its short arm to the end of its long arm, so trimming it reads as a pen.
private struct Tick: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.midY + rect.height * 0.05))
        path.addLine(to: CGPoint(x: rect.minX + rect.width * 0.38, y: rect.maxY - rect.height * 0.12))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.minY + rect.height * 0.15))
        return path
    }
}

/// "4 steps", above a finished reply: a closed line that opens into the list.
private struct StepsSummary: View {
    let companion: Companion
    @StateObject private var hovered = Flag()

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            RoundedRectangle(cornerRadius: 1)
                .fill(Paper.accent.opacity(companion.stepsExpanded ? 0.45 : 0.28))
                .frame(width: 2)
            VStack(alignment: .leading, spacing: 6) {
                Button {
                    companion.stepsExpanded.toggle()
                } label: {
                    HStack(spacing: 4) {
                        Text("\(companion.lastSteps.count) \(companion.lastSteps.count == 1 ? "step" : "steps")")
                        Image(systemName: "chevron.right")
                            .font(.system(size: 8, weight: .bold))
                            .rotationEffect(.degrees(companion.stepsExpanded ? 90 : 0))
                    }
                    .font(Paper.Typography.ledgerMeta)
                    .foregroundStyle(hovered.on ? Paper.inkPrimary : Paper.inkSecondary)
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
                .accessibilityLabel(companion.stepsExpanded ? "Hide bud's steps" : "Show bud's steps")
                if companion.stepsExpanded {
                    StepList(steps: companion.lastSteps, live: false, limit: nil)
                        .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .animation(.spring(duration: 0.25, bounce: 0), value: companion.stepsExpanded)
    }
}

// MARK: Approval card

/// What bud wants to do, in plain words, with what you'd check before saying yes, and the
/// buttons to decide. A question shows its options instead, plus a way to answer in your words.
struct ApprovalCard: View {
    let companion: Companion
    let approval: Approval
    let onDecide: (Bool) -> Void
    @StateObject private var layout = ApprovalLayout()
    @Environment(\.replyType) private var replyType

    private var inWindow: Bool {
        !replyType.compact || companion.windowOpen
    }

    private var busy: Bool {
        companion.deciding.contains(approval.id) || companion.approvingBatch || companion.link != .connected
    }
    private var detailLimit: CGFloat {
        // The browser caution is read in full before turning it on, never behind a scroll;
        // it is written short enough to fit the deck at this height.
        if inWindow {
            return approval.isQuestion ? 260 : 200
        }
        let height: CGFloat = approval.isQuestion ? 190 : approval.tool == "enable_browser" ? 135 : 94
        let hasProblem = companion.approvalFailures[approval.id] != nil || companion.decisionProblem != nil
        return hasProblem ? max(80, height - 40) : height
    }

    var body: some View {
        let radius = inWindow ? Paper.radiusLG : Paper.radiusMD
        Group {
            if inWindow {
                windowContent
            } else {
                compactContent.padding(12)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .surface(
            RoundedRectangle(cornerRadius: radius, style: .continuous),
            lift: 1,
            fill: Paper.elevated,
            border: inWindow ? Paper.borderStrong : Paper.borderSubtle,
            borderWidth: 0.5
        )
        .disabled(busy)
        .opacity(busy ? 0.55 : 1)
        .animation(.easeOut(duration: 0.12), value: busy)
    }

    private var compactContent: some View {
        VStack(alignment: .leading, spacing: 10) {
            heading
            if approval.isQuestion {
                QuestionCards(companion: companion, approval: approval,
                              flow: companion.questionFlow(for: approval), listLimit: detailLimit)
            } else {
                detail
                buttons
            }
            if let problem = companion.approvalFailures[approval.id] ?? companion.decisionProblem {
                Text(problem)
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.errorInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var windowContent: some View {
        VStack(alignment: .leading, spacing: 0) {
            if approval.isQuestion {
                QuestionCards(
                    companion: companion,
                    approval: approval,
                    flow: companion.questionFlow(for: approval),
                    listLimit: detailLimit
                )
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    heading
                    detail
                    if let problem = companion.approvalFailures[approval.id] ?? companion.decisionProblem {
                        Text(problem)
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.errorInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 14)
                .padding(.bottom, 12)

                Rectangle().fill(Paper.hairline).frame(height: 0.5)

                windowFooter
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
            }
        }
    }

    private var windowFooter: some View {
        HStack(spacing: 8) {
            Button("Deny") { onDecide(false) }
                .buttonStyle(CardButton(kind: .secondary))
            if !Approval.yesOrNo.contains(approval.tool) {
                Button("Change…") { companion.write(.revise(approval.id)) }
                    .buttonStyle(CardButton(kind: .quiet))
                    .help("Tell bud what to do differently")
            }
            Spacer(minLength: 4)
            if approval.grantable { alwaysAllow }
            Button(companion.approvalFailures[approval.id] == nil ? approval.verb : "Retry") {
                onDecide(true)
            }
            .buttonStyle(CardButton(kind: .primary))
        }
    }

    private var detail: some View {
        VStack(alignment: .leading, spacing: 10) {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if !approval.facts.isEmpty { facts }
                    if approval.tool == "enable_browser" {
                        BrowserCaution()
                    } else if let text = approval.body, !approval.isQuestion {
                        EmailBody(text: text)
                    }
                    if let email = approval.emailPreview,
                       !email.messages.isEmpty || email.unavailableReason != nil {
                        if approval.body != nil {
                            Divider()
                            Text("Original email")
                                .font(Paper.Typography.captionMedium)
                                .foregroundStyle(Paper.inkSecondary)
                        }
                        EmailThreadPreview(email: email)
                    } else if approval.expectsOriginalEmail && approval.emailPreview == nil {
                        Text("Original email preview unavailable")
                            .font(Paper.Typography.callout)
                            .foregroundStyle(Paper.inkSecondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
                    layout.height = $0
                }
                .padding(.trailing, 6)
            }
            .frame(height: min(layout.height, detailLimit))
            .scrollBounceBehavior(.basedOnSize)
            .textSelection(.enabled)
            if layout.height > detailLimit {
                Text("Scroll to read more")
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.inkSecondary)
            }
        }
    }

    private var heading: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: approval.symbol)
                .font(.system(size: 12, weight: .medium))
                .frame(width: 26, height: 26)
                .background(
                    Paper.fillSubtle,
                    in: RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(approval.isQuestion ? "bud has a question" : approval.title)
                    .font(Fonts.sans(size: 13, weight: Fonts.OpticalWeight.semibold))
                    .lineLimit(2)
                if let job = approval.job {
                    Text("For the job “\(job)”")
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkSecondary)
                        .lineLimit(1)
                }
            }
            .padding(.top, approval.job == nil ? 4 : 0)
            Spacer(minLength: 0)
        }
    }

    private var facts: some View {
        Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 3) {
            ForEach(approval.facts.indices, id: \.self) { index in
                GridRow {
                    Text(approval.facts[index].label).foregroundStyle(Paper.inkSecondary)
                    Text(approval.facts[index].value)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
        }
        .font(Paper.Typography.callout)
    }

    private var buttons: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                // Turning the browser on or opening Chrome has nothing to change: yes or no.
                if !Approval.yesOrNo.contains(approval.tool) {
                    Button("Change…") { companion.write(.revise(approval.id)) }
                        .buttonStyle(CardButton(kind: .quiet))
                        .help("Tell bud what to do differently")
                }
                Spacer(minLength: 0)
                if approval.grantable { alwaysAllow }
            }
            HStack(spacing: 6) {
                Button { onDecide(false) } label: {
                    Text("Deny").frame(maxWidth: .infinity)
                }
                .buttonStyle(CardButton(kind: .secondary))
                Button {
                    onDecide(true)
                } label: {
                    Text(companion.approvalFailures[approval.id] == nil ? approval.verb : "Retry")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CardButton(kind: .primary))
            }
        }
    }

    private var alwaysAllow: some View {
        Button("Always allow") { companion.alwaysAllow(approval) }
            .buttonStyle(CardButton(kind: .quiet))
            .help(approval.alwaysAllowHelp)
            .accessibilityLabel("Always allow")
            .accessibilityHint(approval.alwaysAllowHelp)
    }

}

/// Per-view state without `@State` (a macro whose plugin ships only with Xcode): an
/// observable object owned by the view, which `@StateObject` keeps across redraws.
final class Flag: ObservableObject {
    @Published var on = false
}

struct PaperPress: ViewModifier {
    let pressed: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content.scaleEffect(pressed && !reduceMotion && (NSEvent.pressedMouseButtons & 1) != 0 ? 0.97 : 1)
    }
}

/// Feedback for controls whose label already defines its layout.
struct PaperHit<S: Shape>: ButtonStyle {
    let shape: S
    var fill: Color = .clear

    func makeBody(configuration: Configuration) -> some View {
        Face(configuration: configuration, shape: shape, fill: fill)
    }

    private struct Face: View {
        let configuration: Configuration
        let shape: S
        let fill: Color
        @StateObject private var hover = Flag()
        @Environment(\.isEnabled) private var enabled

        var body: some View {
            configuration.label
                .background(fill, in: shape)
                .overlay { shape.fill(enabled && hover.on ? Paper.fillHover : .clear).allowsHitTesting(false) }
                .contentShape(shape)
                .pointerStyle(enabled ? .link : .default)
                .onHover { hover.on = $0 }
                .modifier(PaperPress(pressed: enabled && configuration.isPressed))
                .opacity(enabled ? 1 : 0.45)
        }
    }
}

struct IconButton: ButtonStyle {
    var size: CGFloat = 24
    var highlight = true

    func makeBody(configuration: Configuration) -> some View {
        Face(configuration: configuration, size: size, highlight: highlight)
    }

    private struct Face: View {
        let configuration: Configuration
        let size: CGFloat
        let highlight: Bool
        @StateObject private var hover = Flag()
        @Environment(\.isEnabled) private var enabled

        var body: some View {
            let hovered = enabled && hover.on
            configuration.label
                .foregroundStyle(!highlight ? Paper.canvas : hovered ? Paper.inkPrimary : Paper.inkSecondary)
                .frame(width: size, height: size)
                .background(highlight && hovered ? Paper.fillHover : .clear, in: Circle())
                .contentShape(Circle())
                .modifier(PaperPress(pressed: enabled && configuration.isPressed))
                .pointerStyle(enabled ? .link : .default)
                .onHover { hover.on = $0 }
        }
    }
}

struct CardButton: ButtonStyle {
    enum Kind { case primary, secondary, quiet }
    let kind: Kind

    func makeBody(configuration: Configuration) -> some View {
        Face(configuration: configuration, kind: kind)
    }

    private struct Face: View {
        let configuration: Configuration
        let kind: Kind
        @StateObject private var hover = Flag()
        @Environment(\.isEnabled) private var enabled

        var body: some View {
            let shape = RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
            configuration.label
                .font(Paper.Typography.label)
                .foregroundStyle(ink)
                .padding(.horizontal, kind == .quiet ? 6 : 12)
                .frame(height: 26)
                .background {
                    switch kind {
                    case .primary: shape.fill(Paper.inkPrimary.opacity(highlighted ? 0.88 : 1))
                    case .secondary:
                        shape.fill(highlighted ? Paper.fillHover : Paper.fillSubtle)
                            .overlay(shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5))
                    case .quiet: shape.fill(highlighted ? Paper.fillHover : .clear)
                    }
                }
                .contentShape(shape)
                .modifier(PaperPress(pressed: enabled && configuration.isPressed))
                .pointerStyle(enabled ? .link : .default)
                .onHover { hover.on = $0 }
        }

        private var highlighted: Bool { enabled && hover.on }

        private var ink: AnyShapeStyle {
            switch kind {
            case .primary: AnyShapeStyle(Paper.canvas)
            case .secondary: AnyShapeStyle(Paper.inkPrimary)
            case .quiet: highlighted ? AnyShapeStyle(Paper.inkPrimary) : AnyShapeStyle(Paper.inkSecondary)
            }
        }
    }
}

/// One option of a question: a full-width row, so the whole line is the target. On hover the
/// row darkens and its arrow nudges forward, pointing at what a click will do.
struct ChoiceRow: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        Face(configuration: configuration)
    }

    private struct Face: View {
        let configuration: Configuration
        @StateObject private var hover = Flag()

        var body: some View {
            let shape = RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
            configuration.label
                .environment(\.choiceHovered, hover.on)
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    configuration.isPressed ? Paper.fillPressed : hover.on ? Paper.fillHover : Paper.fillSubtle,
                    in: shape)
                .overlay(shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5))
                .contentShape(shape)
                .modifier(PaperPress(pressed: configuration.isPressed))
                .pointerStyle(.link)
                .onHover { hover.on = $0 }
        }
    }
}

private struct ChoiceHoveredKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var choiceHovered: Bool {
        get { self[ChoiceHoveredKey.self] }
        set { self[ChoiceHoveredKey.self] = newValue }
    }
}

/// The row's trailing symbol, which steps forward while its row is hovered.
struct RowSymbol: View {
    let name: String
    @Environment(\.choiceHovered) private var hovered

    var body: some View {
        Image(systemName: name)
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(hovered ? Paper.inkSecondary : Paper.inkMuted)
            .offset(x: hovered ? 2 : 0)
    }
}

/// Items of a set arriving one after another, 40 ms apart: read as a list, not a block.
struct Stagger: ViewModifier {
    let index: Int
    @StateObject private var shown = Flag()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .opacity(shown.on ? 1 : 0)
            .offset(y: shown.on || reduceMotion ? 0 : 4)
            .onAppear {
                withAnimation(.spring(duration: 0.3, bounce: 0).delay(0.06 + Double(index) * 0.04)) {
                    shown.on = true
                }
            }
    }
}

/// What a decided card leaves behind for a moment: the outcome, in a word or two.
struct ReceiptLine: View {
    let receipt: Receipt

    var body: some View {
        Label {
            Text(receipt.text)
                .font(Fonts.sans(size: 13, weight: Fonts.OpticalWeight.medium))
        } icon: {
            Image(systemName: receipt.symbol)
                .font(.system(size: 10, weight: .bold))
                .frame(width: 18, height: 18)
                .background(Paper.fillSubtle, in: Circle())
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

/// A rounded bubble with a small tail pointing at the pet's centre: down from the bubble's
/// bottom edge, or up from its top when the bubble opens below the pet.
struct BubbleShape: Shape {
    static let tail: CGFloat = 7
    var anchor = Anchor()
    /// The pet is 96 pt wide and lines up with the bubble's edge, so its centre is 48 pt in.
    var tailInset: CGFloat = 48
    var radius: CGFloat = 14

    func path(in rect: CGRect) -> Path {
        let body = CGRect(
            x: rect.minX, y: rect.minY + (anchor.below ? Self.tail : 0),
            width: rect.width, height: rect.height - Self.tail)
        let x = anchor.left ? body.minX + tailInset : body.maxX - tailInset
        // Drawn pointing down; flipped about the bubble's edge when it points up.
        let edge = anchor.below ? body.minY : body.maxY
        let out: CGFloat = anchor.below ? -1 : 1
        func at(_ dx: CGFloat, _ dy: CGFloat) -> CGPoint { CGPoint(x: x + dx, y: edge + dy * out) }
        let half: CGFloat = 8
        var path = Path(roundedRect: body, cornerRadius: radius, style: .continuous)
        // The tail joins the edge with soft shoulders, so it reads as one shape.
        var tail = Path()
        tail.move(to: at(-half - 4, 0))
        tail.addQuadCurve(to: at(-1.5, Self.tail - 1), control: at(-half + 2, 0))
        tail.addQuadCurve(to: at(1.5, Self.tail - 1), control: at(0, Self.tail))
        tail.addQuadCurve(to: at(half + 4, 0), control: at(half - 2, 0))
        tail.closeSubpath()
        path = path.union(tail)
        return path
    }
}

extension View {
    /// A soft band of light moving across text: busy without a spinner. Still when reduced.
    func shimmer(active: Bool) -> some View {
        modifier(Shimmer(active: active))
    }
}

private struct Shimmer: ViewModifier {
    let active: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        // One structure whether on or off, so the text keeps its identity when the shimmer
        // stops and anything animating on it (a step's cross-out line) plays through.
        content.overlay {
            if active && !reduceMotion {
                TimelineView(.animation) { context in
                    // One sweep every 1.6 s, linear: it's ambient, not an event.
                    let phase = context.date.timeIntervalSinceReferenceDate
                        .truncatingRemainder(dividingBy: 1.6) / 1.6
                    LinearGradient(
                        stops: [.init(color: .clear, location: 0),
                                .init(color: Paper.inkPrimary.opacity(0.85), location: 0.5),
                                .init(color: .clear, location: 1)],
                        startPoint: UnitPoint(x: phase * 3 - 1.5, y: 0.5),
                        endPoint: UnitPoint(x: phase * 3 - 0.5, y: 0.5))
                }
                .mask(content)
                .allowsHitTesting(false)
            }
        }
    }
}

// MARK: Input

/// Quiet 11pt mono keybind chip on recessed paper with a 0.5pt hairline border.
struct KeybindChip: View {
    let key: String

    init(_ key: String) {
        self.key = key
    }

    var body: some View {
        Text(key)
            .font(Paper.Typography.ledgerMeta)
            .foregroundStyle(Paper.inkMuted)
            .padding(.horizontal, 5)
            .padding(.vertical, 2)
            .background(
                Paper.recessed,
                in: RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous)
                    .strokeBorder(Paper.borderSubtle, lineWidth: 0.5))
            .accessibilityHidden(true)
    }
}

struct RoundButton: View {
    let symbol: String
    let label: String
    var size: CGFloat = 26
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: size >= 26 ? 11 : 10, weight: .bold))
                .frame(width: size, height: size)
                .background(Paper.inkPrimary, in: Circle())
        }
        .buttonStyle(IconButton(size: size, highlight: false))
        .help(label)
        .accessibilityLabel(label)
    }
}

/// Opens without animation: you open it often, often from the keyboard.
private struct InputField: View {
    @Bindable var companion: Companion
    @FocusState private var focused: Bool
    @StateObject private var droppingAttachments = Flag()

    var body: some View {
        let hasText = !companion.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let hasFiles = companion.inputPurpose == .ask && !companion.draftAttachments.isEmpty
        let dropTargeted = droppingAttachments.on && companion.canAddAttachments
        let highlighted = focused || dropTargeted
        let shape = RoundedRectangle(cornerRadius: Paper.radiusLG, style: .continuous)
        VStack(alignment: .leading, spacing: 6) {
            if hasFiles {
                DraftAttachmentChips(files: companion.draftAttachments, remove: companion.removeAttachment)
            }
            inputRow(canSend: hasText || hasFiles)
        }
        .padding(.leading, 14)
        .padding(.trailing, 8)
        .padding(.vertical, 8)
        .foregroundStyle(Paper.inkPrimary)
        .frame(width: 332)
        .surface(
            shape,
            lift: 1,
            fill: dropTargeted ? Paper.overlay : Paper.elevated,
            border: highlighted ? Paper.accent : Paper.borderSubtle,
            borderWidth: dropTargeted ? 2 : highlighted ? 1 : 0.5
        )
        .dropAttachments(into: companion, isTargeted: $droppingAttachments.on)
        .animation(.easeOut(duration: 0.12), value: focused)
        .onAppear { focused = true }
    }

    private func inputRow(canSend: Bool) -> some View {
        HStack(alignment: .bottom, spacing: 8) {
            ZStack(alignment: .leading) {
                if companion.draft.isEmpty {
                    Text(companion.inputPurpose.prompt)
                        .font(Paper.Typography.body)
                        .foregroundStyle(Paper.placeholder)
                        .lineLimit(1)
                        .allowsHitTesting(false)
                }
                ChatInput(title: "", text: $companion.draft, focus: $focused, submit: { companion.send() })
                    .font(Paper.Typography.body)
                    .lineLimit(1...5)
                    .onExitCommand {
                        companion.inputOpen = false
                        companion.inputPurpose = .ask
                    }
            }
            .padding(.vertical, 3)

            if companion.inputPurpose == .ask && (companion.readingAttachments || companion.uploadingAttachments) {
                ProgressView().controlSize(.small)
                    .accessibilityLabel(companion.readingAttachments ? "Reading files" : "Uploading files")
            }
            RoundButton(symbol: "arrow.up", label: "Send", size: 22, action: { companion.send() })
                .opacity(canSend ? 1 : 0.35)
        }
    }
}

// MARK: Settings

/// Editable copy of the settings. A shared object, not `@State`: SwiftUI's `@State` is a macro
/// whose plugin ships with Xcode, and this app builds with the Command Line Tools alone.
@MainActor @Observable
final class SettingsDraft {
    static let shared = SettingsDraft()
    var settings = BudSettings.load()
}

/// Settings: where bud runs, and the folder its token lives in.
struct SettingsView: View {
    @Bindable var draft = SettingsDraft.shared

    var body: some View {
        Form {
            TextField("bud URL", text: $draft.settings.url)
            TextField("bud folder", text: $draft.settings.home, prompt: Text("/path/to/bud"))
            Text("The folder that holds bud's .env and data. The owner token is read from there.")
                .font(Paper.Typography.callout)
                .foregroundStyle(Paper.inkSecondary)
            Button("Save and reconnect") {
                draft.settings.save()
                Companion.shared.start()
            }
        }
        .formStyle(.grouped)
        .frame(width: 460)
        .onAppear { draft.settings = BudSettings.load() }
    }
}

/// Opens a finished job's saved report in the app the owner uses for that file type.
struct OpenReport: View {
    let file: URL

    var body: some View {
        Button("Open report") { NSWorkspace.shared.open(file) }
            .buttonStyle(.link)
            .pointerStyle(.link)
            .font(Paper.Typography.callout)
            .padding(.leading, 22)
            .help(file.path)
            .accessibilityLabel("Open report \(file.lastPathComponent)")
    }
}

/// The one-time browser card: a short summary, with the full caution a click away.
private struct BrowserCaution: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(Approval.browserSummary)
                .font(Paper.Typography.callout)
                .fixedSize(horizontal: false, vertical: true)
            DisclosureGroup("What this allows") {
                Text(Approval.browserCaution)
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 4)
            }
            .font(Paper.Typography.label)
            .pointerStyle(.link)
        }
    }
}
