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
        HStack(spacing: 10) {
            if anchor.left { pet }
            if Voice.shared.state == .listening || Voice.shared.state == .transcribing {
                VoicePill(voice: Voice.shared)
                    .transition(.opacity.combined(with: .scale(scale: 0.9, anchor: anchor.left ? .leading : .trailing)))
            } else if companion.running {
                StopButton(action: companion.stop)
                    .transition(.opacity)
            }
            if !anchor.left { pet }
        }
        .animation(.spring(duration: 0.25, bounce: 0), value: Voice.shared.state)
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
                    // Always mounted, so it can play on the very arrival that brings the dot.
                    Ripple(trigger: companion.arrivals)
                    if companion.status == .needsYou && connected {
                        Circle().fill(.orange)
                            .overlay(Circle().strokeBorder(.white.opacity(0.9), lineWidth: 1.5))
                            .transition(.scale(scale: 0.6).combined(with: .opacity))
                    }
                }
                .frame(width: 12, height: 12)
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

/// One ring spreading from the dot when something new needs you. Once per arrival, never a
/// loop: a repeating pulse would turn into noise you learn to ignore. Still when reduced.
private struct Ripple: View {
    let trigger: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        if !reduceMotion {
            Circle()
                .stroke(.orange, lineWidth: 1.5)
                .keyframeAnimator(initialValue: 1.0, trigger: trigger) { ring, t in
                    ring.scaleEffect(1 + t * 1.8).opacity(t >= 1 ? 0 : 0.7 * (1 - t))
                } keyframes: { _ in
                    LinearKeyframe(0, duration: 0.01)
                    CubicKeyframe(1, duration: 0.8)
                }
        }
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
                Circle().strokeBorder(.secondary, style: StrokeStyle(lineWidth: 2, dash: [3, 4]))
            } else {
                switch status {
                case .working:
                    working
                case .needsYou:
                    Circle().strokeBorder(.orange, lineWidth: 2)
                    Circle().fill(.orange).frame(width: 12, height: 12)
                        .overlay(Circle().strokeBorder(.background, lineWidth: 2))
                        .offset(x: 20, y: -20)
                case .failed:
                    Circle().strokeBorder(.red, lineWidth: 2)
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
            Circle().strokeBorder(.secondary.opacity(0.6), lineWidth: 2)
        } else {
            TimelineView(.animation) { context in
                let turn = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 0.9) / 0.9
                Circle()
                    .trim(from: 0, to: 0.28)
                    .stroke(.secondary, style: StrokeStyle(lineWidth: 2, lineCap: .round))
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
                        .fill(Paper.ink.opacity(0.75))
                        .frame(width: 3, height: 4 + CGFloat(voice.level * shape[index]) * 14)
                }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.08), value: voice.level)
            } else {
                Text("Writing")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
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
                .font(.callout)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(companion.inputPurpose == .ask ? "Send" : "Add to answer") { companion.resendSpeech() }
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
                    .font(.callout)
                    .foregroundStyle(.secondary)
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
        VStack(alignment: .leading, spacing: 10) {
            header
            if !compact, !companion.question.isEmpty, companion.setups.isEmpty {
                // What you asked, so the answer keeps its context. One line: a reminder.
                Text(companion.question)
                    .font(.callout)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .padding(.top, -4)
                    .transition(.opacity)
            }
            if companion.running, companion.stepsLayout == .checklist, !companion.steps.isEmpty {
                StepList(steps: companion.steps, live: true)
            }
            if companion.showStepsAfter, !companion.running, !companion.lastSteps.isEmpty {
                StepsSummary(companion: companion)
            }
            ForEach(companion.notices, id: \.self) { notice in
                Label(notice, systemImage: "arrow.turn.down.right")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
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
                    MarkdownView(companion.shown)
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
        .foregroundStyle(Paper.ink)
        .padding(.horizontal, 16)
        // The tail's edge gets its extra room, toward the pet.
        .padding(.top, 12 + (companion.anchor.below ? BubbleShape.tail : 0))
        .padding(.bottom, 14 + (companion.anchor.below ? 0 : BubbleShape.tail))
        // Only the name line: a small tag over the pet, not a wide empty card.
        .frame(width: compact ? nil : 320, alignment: .leading)
        .frame(minWidth: 132, alignment: .leading)
        .fixedSize(horizontal: compact, vertical: false)
        .surface(BubbleShape(anchor: companion.anchor))
        .onHover { companion.bubbleHovered = $0 }
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
    private var maxReply: CGFloat { companion.needsAttention ? 60 : 260 }

    /// Who is speaking and what they're doing, like a name above a message.
    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("bud")
                .font(.system(size: 13, weight: .semibold, design: .serif))
            if let state {
                Text(state.text)
                    .font(.system(size: 12))
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
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.tertiary)
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
        chrome("xmark", label: "Close reply") { companion.bubbleOpen = false }
    }

    /// Opens the whole conversation in a window, for long replies and earlier messages.
    private var expand: some View {
        chrome("arrow.up.left.and.arrow.down.right", label: "Open in a window") {
            ConversationWindow.shared.show()
        }
        .help("Open in a window")
    }

    private func chrome(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 9, weight: .semibold))
        }
        .buttonStyle(IconButton(size: 20))
        .opacity(companion.bubbleHovered ? 1 : 0)
        .animation(.easeOut(duration: 0.12), value: companion.bubbleHovered)
        .accessibilityLabel(label)
    }

    /// Plain words for what bud is doing. Colour only when it's your move or something broke.
    private var state: (text: String, tint: Color, busy: Bool)? {
        if case .problem(let detail) = companion.link {
            return (detail, .secondary, false)
        }
        switch companion.status {
        case .working:
            // The live line: the step bud is on, unless the checklist below already shows it.
            if companion.stepsLayout != .checklist, let step = companion.steps.last {
                return (step.text, .secondary, true)
            }
            return ("Working on it", .secondary, true)
        case .needsYou:
            if !companion.setups.isEmpty { return ("Needs you", .orange, false) }
            let asking = companion.hasQuestion
            return (asking ? "Has a question" : "Needs your OK", .orange, false)
        case .failed: return ("Couldn't finish", .red, false)
        case .done: return ("Done", .secondary, false)
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
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .padding(.leading, 22)
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
/// inside it, and a line crosses the words out, left to right. Steps from the same call
/// finish together, so each ticks 70 ms after the one above it and they read as a run of
/// ticks, not one blink. Rows that arrive already done (the list after a reply) don't play it.
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
                    .strokeBorder(Paper.ink.opacity(0.25), lineWidth: 1)
                    .opacity(step.done ? 0 : 1)
                Circle()
                    .fill(Paper.ink.opacity(0.8))
                    .scaleEffect(step.done || reduceMotion ? 1 : 0.6)
                    .opacity(step.done ? 1 : 0)
                Tick()
                    .trim(from: 0, to: step.done || reduceMotion ? 1 : 0)
                    .stroke(Paper.surface, style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
                    .opacity(step.done ? 1 : 0)
                    .padding(3)
            }
            .frame(width: 12, height: 12)
            .alignmentGuide(.firstTextBaseline) { $0[VerticalAlignment.center] + 4 }
            .animation(check, value: step.done)

            Text(step.text)
                .font(.callout)
                .foregroundStyle(live ? .primary : .secondary)
                .lineLimit(1)
                .truncationMode(.tail)
                .shimmer(active: live)
                .overlay(alignment: .leading) {
                    Rectangle()
                        .fill(Paper.ink.opacity(0.35))
                        .frame(height: 1)
                        .scaleEffect(x: step.done || reduceMotion ? 1 : 0, anchor: .leading)
                        .opacity(step.done ? 1 : 0)
                        // The line follows the tick, so the eye goes circle, then words.
                        .animation(reduceMotion ? check : check.delay(0.1), value: step.done)
                }
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

    var body: some View {
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
                .font(.caption)
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(companion.stepsExpanded ? "Hide bud's steps" : "Show bud's steps")
            if companion.stepsExpanded {
                StepList(steps: companion.lastSteps, live: false, limit: nil)
                    .transition(.opacity)
            }
        }
        .animation(.spring(duration: 0.25, bounce: 0), value: companion.stepsExpanded)
    }
}

// MARK: Approval card

/// What bud wants to do, in plain words, with what you'd check before saying yes, and the
/// buttons to decide. A question shows its options instead, plus a way to answer in your words.
struct ApprovalCard: View {
    let companion: Companion
    let approval: Approval
    /// Inside a collapsible row: the row draws the surface, so the card draws only its content.
    let nested: Bool
    let showsHeading: Bool
    @StateObject private var layout = ApprovalLayout()

    private var busy: Bool {
        companion.deciding.contains(approval.id) || companion.approvingBatch || companion.link != .connected
    }
    private var detailLimit: CGFloat {
        // Several rows share one floating panel, so an open card gives up some height.
        let crowded = companion.approvals.count > 2
        let height: CGFloat = companion.windowOpen ? 300 : companion.inputOpen ? 140 : crowded ? 130 : 190
        let hasProblem = companion.approvalFailures[approval.id] != nil || companion.decisionProblem != nil
        return hasProblem ? max(80, height - 40) : height
    }

    var body: some View {
        Group {
            if nested {
                content
            } else {
                content
                    .padding(12)
                    .background(Paper.ink.opacity(0.035),
                                in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(Paper.hairline, lineWidth: 0.5))
            }
        }
        .disabled(busy)
        .opacity(busy ? 0.55 : 1)
        .animation(.easeOut(duration: 0.12), value: busy)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 10) {
            if showsHeading { heading }
            if approval.isQuestion {
                QuestionCards(companion: companion, approval: approval,
                              flow: companion.questionFlow(for: approval), listLimit: detailLimit)
            } else {
                detail
                buttons
            }
            if let problem = companion.approvalFailures[approval.id] ?? companion.decisionProblem {
                Text(problem)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var detail: some View {
        VStack(alignment: .leading, spacing: 10) {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if !approval.facts.isEmpty { facts }
                    if let text = approval.body, !approval.isQuestion { EmailBody(text: text) }
                    if let email = approval.emailPreview,
                       !email.messages.isEmpty || email.unavailableReason != nil {
                        if approval.body != nil {
                            Divider()
                            Text("Original email").font(.caption.weight(.medium)).foregroundStyle(.secondary)
                        }
                        EmailThreadPreview(email: email)
                    } else if approval.expectsOriginalEmail && approval.emailPreview == nil {
                        Text("Original email preview unavailable")
                            .font(.callout)
                            .foregroundStyle(.secondary)
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
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var heading: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: approval.symbol)
                .font(.system(size: 12, weight: .medium))
                .frame(width: 26, height: 26)
                .background(Paper.ink.opacity(0.07), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(approval.isQuestion ? "bud has a question" : approval.title)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(2)
                if let job = approval.job {
                    Text("For the job “\(job)”")
                        .font(.caption)
                        .foregroundStyle(.secondary)
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
                    Text(approval.facts[index].label).foregroundStyle(.secondary)
                    Text(approval.facts[index].value)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
        }
        .font(.callout)
    }

    private var buttons: some View {
        VStack(alignment: .trailing, spacing: 6) {
            HStack(spacing: 6) {
                Button("Change…") { companion.write(.revise(approval.id)) }
                    .buttonStyle(CardButton(kind: .quiet))
                    .help("Tell bud what to do differently")
                Spacer(minLength: 0)
                Button("Deny") { companion.decide(approval.id, approved: false) }
                    .buttonStyle(CardButton(kind: .secondary))
                Button(companion.approvalFailures[approval.id] == nil ? approval.verb : "Retry") {
                    companion.decide(approval.id, approved: true)
                }
                    .buttonStyle(CardButton(kind: .primary))
            }
            if approval.grantable { alwaysAllow }
        }
    }

    /// Under the decision, not beside it: three capsules in one row overrun the bubble, and a
    /// truncated "Always al…" would hide what the click gives away.
    private var alwaysAllow: some View {
        Button("Always allow") { companion.alwaysAllow(approval) }
            .buttonStyle(CardButton(kind: .secondary))
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

/// Small icon buttons answer the pointer: a soft round highlight fades in, the icon darkens
/// and grows a touch, and it presses in on click. Fast and small, since the pointer often
/// only passes by. `highlight: false` keeps a button's own look (a filled circle) and only
/// adds the motion. Reduced motion: colour only, no size change.
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
        @Environment(\.accessibilityReduceMotion) private var reduceMotion

        var body: some View {
            configuration.label
                .foregroundStyle(ink)
                .frame(width: size, height: size)
                .background(Circle().fill(Paper.ink.opacity(highlight && hover.on ? 0.07 : 0)))
                .contentShape(Circle())
                .scaleEffect(scale)
                .onHover { hover.on = $0 }
                .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
                .animation(.easeOut(duration: 0.12), value: hover.on)
        }

        private var ink: AnyShapeStyle {
            guard highlight else { return AnyShapeStyle(Paper.surface) }
            return hover.on ? AnyShapeStyle(Paper.ink) : AnyShapeStyle(.secondary)
        }

        private var scale: CGFloat {
            if reduceMotion { return 1 }
            if configuration.isPressed { return 0.9 }
            return hover.on ? 1.08 : 1
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

        var body: some View {
            configuration.label
                .font(.system(size: 12.5, weight: .medium))
                .foregroundStyle(ink)
                .padding(.horizontal, kind == .quiet ? 6 : 12)
                .frame(height: 26)
                .background {
                    switch kind {
                    case .primary: Capsule().fill(Paper.ink.opacity(hover.on ? 0.86 : 1))
                    case .secondary: Capsule().fill(Paper.ink.opacity(hover.on ? 0.11 : 0.07))
                    case .quiet: Capsule().fill(Paper.ink.opacity(hover.on ? 0.06 : 0))
                    }
                }
                .contentShape(Capsule())
                .scaleEffect(configuration.isPressed ? 0.97 : 1)
                .onHover { hover.on = $0 }
                // Press is instant feedback; hover eases, since the pointer only passes by.
                .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
                .animation(.easeOut(duration: 0.12), value: hover.on)
        }

        private var ink: AnyShapeStyle {
            switch kind {
            case .primary: AnyShapeStyle(Paper.surface)
            case .secondary: AnyShapeStyle(Paper.ink)
            case .quiet: hover.on ? AnyShapeStyle(Paper.ink) : AnyShapeStyle(.secondary)
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
            configuration.label
                .environment(\.choiceHovered, hover.on)
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Paper.ink.opacity(configuration.isPressed ? 0.13 : hover.on ? 0.09 : 0.05),
                            in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                .contentShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                .scaleEffect(configuration.isPressed ? 0.985 : 1)
                .onHover { hover.on = $0 }
                .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
                .animation(.easeOut(duration: 0.12), value: hover.on)
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
            .foregroundStyle(hovered ? .secondary : .tertiary)
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
            Text(receipt.text).font(.system(size: 13, weight: .medium))
        } icon: {
            Image(systemName: receipt.symbol)
                .font(.system(size: 10, weight: .bold))
                .frame(width: 18, height: 18)
                .background(Paper.ink.opacity(0.07), in: Circle())
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
                                .init(color: Paper.ink.opacity(0.85), location: 0.5),
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

/// Opens without animation: you open it often, often from the keyboard.
private struct InputField: View {
    @Bindable var companion: Companion
    @FocusState private var focused: Bool

    var body: some View {
        TextField(companion.inputPurpose.prompt, text: $companion.draft, axis: .vertical)
            .textFieldStyle(.plain)
            .font(.body)
            .lineLimit(1...5)
            .focused($focused)
            .onSubmit { companion.send() }
            .onExitCommand {
                companion.inputOpen = false
                companion.inputPurpose = .ask
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .foregroundStyle(Paper.ink)
            .frame(width: 320)
            .surface(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .onAppear { focused = true }
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
                .font(.callout)
                .foregroundStyle(.secondary)
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
