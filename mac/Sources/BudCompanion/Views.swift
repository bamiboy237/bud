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
                if companion.inputOpen { InputField(companion: companion) }
                bubble
                Spacer(minLength: 0)
            } else {
                Spacer(minLength: 0)
                bubble
                if companion.inputOpen { InputField(companion: companion) }
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
        if companion.bubbleOpen {
            Bubble(companion: companion)
                .transition(bubbleTransition)
        }
    }

    /// The stop button sits on the bubble's side of the pet.
    private var petRow: some View {
        HStack(spacing: 10) {
            if anchor.left { pet }
            if companion.status == .working {
                StopButton(action: companion.stop)
                    .transition(.opacity)
            }
            if !anchor.left { pet }
        }
    }

    private var pet: some View {
                PetButton(companion: companion) {
                    // The release that ends a drag isn't a click.
                    if companion.dragging { return }
                    // Something waiting comes first: a click shows it rather than a blank field.
                    if !companion.approvals.isEmpty && !companion.bubbleOpen {
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
        AnimatedPet(sprite: sprite, pose: companion.pose(at:))
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

// MARK: Bubble

private struct Bubble: View {
    @Bindable var companion: Companion

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            ForEach(companion.notices, id: \.self) { notice in
                Label(notice, systemImage: "arrow.turn.down.right")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            if !companion.shown.isEmpty {
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
            if let approval = companion.approvals.first {
                ApprovalCard(
                    companion: companion, approval: approval,
                    position: companion.approvals.count > 1 ? "1 of \(companion.approvals.count)" : nil)
                    .id(approval.id)
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
        .animation(.spring(duration: 0.3, bounce: 0), value: companion.approvals.first?.id)
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
        companion.shown.isEmpty && companion.notices.isEmpty && companion.approvals.isEmpty
            && companion.receipt == nil
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
    private var maxReply: CGFloat { companion.approvals.isEmpty ? 260 : 140 }

    /// Who is speaking and what they're doing, like a name above a message.
    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("bud")
                .font(.system(size: 13, weight: .semibold, design: .serif))
            if let state {
                Text(state.text)
                    .font(.system(size: 12))
                    .foregroundStyle(state.tint)
                    .shimmer(active: state.busy)
                    .contentTransition(.opacity)
            }
            Spacer(minLength: compact ? 6 : 0)
            close
        }
        .animation(.easeOut(duration: 0.15), value: state?.text)
    }

    /// Shown only on hover: less chrome on a surface that floats over your work.
    private var close: some View {
        Button {
            companion.bubbleOpen = false
        } label: {
            Image(systemName: "xmark")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(.secondary)
                .frame(width: 18, height: 18)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .opacity(companion.bubbleHovered ? 1 : 0)
        .animation(.easeOut(duration: 0.12), value: companion.bubbleHovered)
        .accessibilityLabel("Close reply")
    }

    /// Plain words for what bud is doing. Colour only when it's your move or something broke.
    private var state: (text: String, tint: Color, busy: Bool)? {
        if case .problem(let detail) = companion.link {
            return (detail, .secondary, false)
        }
        switch companion.status {
        case .working: return ("Working on it", .secondary, true)
        case .needsYou:
            let asking = companion.approvals.first?.isQuestion == true
            return (asking ? "Has a question" : "Needs your OK", .orange, false)
        case .failed: return ("Couldn't finish", .red, false)
        case .done: return ("Done", .secondary, false)
        case .idle: return nil
        }
    }
}

// MARK: Approval card

/// What bud wants to do, in plain words, with what you'd check before saying yes, and the
/// buttons to decide. A question shows its options instead, plus a way to answer in your words.
private struct ApprovalCard: View {
    let companion: Companion
    let approval: Approval
    /// "1 of 3" when more are waiting behind this one.
    let position: String?

    private var busy: Bool { companion.deciding.contains(approval.id) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            heading
            if !approval.facts.isEmpty { facts }
            if let text = approval.body, !approval.isQuestion { preview(text) }
            if approval.isQuestion { choices } else { buttons }
            if let problem = companion.decisionProblem {
                Text(problem)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .background(Paper.ink.opacity(0.035), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(Paper.hairline, lineWidth: 0.5))
        .disabled(busy)
        .opacity(busy ? 0.55 : 1)
        .animation(.easeOut(duration: 0.12), value: busy)
    }

    private var heading: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: approval.symbol)
                .font(.system(size: 12, weight: .medium))
                .frame(width: 26, height: 26)
                .background(Paper.ink.opacity(0.07), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(approval.title.isEmpty ? "bud has a question" : approval.title)
                    .font(.system(size: 13, weight: .semibold))
                    .fixedSize(horizontal: false, vertical: true)
                if let job = approval.job {
                    Text("For the job “\(job)”")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            .padding(.top, approval.job == nil ? 4 : 0)
            Spacer(minLength: 0)
            if let position {
                Text(position)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.tertiary)
                    .padding(.top, 5)
            }
        }
    }

    private var facts: some View {
        Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 3) {
            ForEach(approval.facts.indices, id: \.self) { index in
                GridRow {
                    Text(approval.facts[index].label).foregroundStyle(.secondary)
                    Text(approval.facts[index].value)
                        .lineLimit(2)
                        .textSelection(.enabled)
                }
            }
        }
        .font(.callout)
    }

    /// What will go out, so you read it before it's sent. Short at first; click for the rest.
    private func preview(_ text: String) -> some View {
        let open = companion.expandedApproval == approval.id
        return Text(text)
            .font(.callout)
            .foregroundStyle(.secondary)
            .lineLimit(open ? nil : 4)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.leading, 10)
            .overlay(alignment: .leading) {
                RoundedRectangle(cornerRadius: 1).fill(Paper.hairline).frame(width: 2)
            }
            .contentShape(Rectangle())
            .onTapGesture { companion.expandedApproval = open ? nil : approval.id }
            .help(open ? "Show less" : "Show all")
    }

    private var buttons: some View {
        HStack(spacing: 6) {
            Button("Change…") { companion.write(.revise(approval.id)) }
                .buttonStyle(CardButton(kind: .quiet))
                .help("Tell bud what to do differently")
            Spacer(minLength: 0)
            Button("Deny") { companion.decide(approval.id, approved: false) }
                .buttonStyle(CardButton(kind: .secondary))
            Button(approval.verb) { companion.decide(approval.id, approved: true) }
                .buttonStyle(CardButton(kind: .primary))
        }
    }

    private var choices: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(approval.choices.enumerated()), id: \.offset) { index, choice in
                Button { companion.choose(choice, for: approval.id) } label: {
                    row(choice, symbol: "arrow.right", tone: .primary)
                }
                .buttonStyle(ChoiceRow())
                .modifier(Stagger(index: index))
            }
            Button { companion.write(.answer(approval.id)) } label: {
                row(approval.choices.isEmpty ? "Answer…" : "Something else…", symbol: "pencil", tone: .secondary)
            }
            .buttonStyle(ChoiceRow())
            .modifier(Stagger(index: approval.choices.count))
        }
    }

    private func row(_ text: String, symbol: String, tone: HierarchicalShapeStyle) -> some View {
        HStack(spacing: 8) {
            Text(text)
                .foregroundStyle(tone)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            RowSymbol(name: symbol)
        }
        .font(.callout)
    }
}

/// Per-view state without `@State` (a macro whose plugin ships only with Xcode): an
/// observable object owned by the view, which `@StateObject` keeps across redraws.
final class Flag: ObservableObject {
    @Published var on = false
}

private struct CardButton: ButtonStyle {
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
private struct ChoiceRow: ButtonStyle {
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
private struct RowSymbol: View {
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
private struct Stagger: ViewModifier {
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
private struct ReceiptLine: View {
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
        if active && !reduceMotion {
            content.overlay {
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
        } else {
            content
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
            .onSubmit(companion.send)
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
