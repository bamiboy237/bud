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

    var body: some View {
        VStack(alignment: .trailing, spacing: 8) {
            Spacer(minLength: 0)
            if companion.bubbleOpen {
                Bubble(companion: companion)
                    .transition(bubbleTransition)
            }
            if companion.inputOpen {
                InputField(companion: companion)
            }
            HStack(spacing: 10) {
                if companion.status == .working {
                    StopButton(action: companion.stop)
                        .transition(.opacity)
                }
                PetButton(companion: companion) {
                    companion.inputOpen.toggle()
                    if companion.inputOpen { focusPanel() }
                }
            }
        }
        .padding(12)
        .frame(width: PetPanel.size.width, height: PetPanel.size.height, alignment: .bottomTrailing)
        .animation(companion.bubbleOpen ? appear : leave, value: companion.bubbleOpen)
        .animation(leave, value: companion.status)
    }

    /// Grows out of the pet's corner, so it reads as the pet speaking. Reduced motion: fade only.
    private var bubbleTransition: AnyTransition {
        reduceMotion
            ? .opacity
            : .asymmetric(
                insertion: .scale(scale: 0.96, anchor: .bottomTrailing).combined(with: .opacity),
                removal: .opacity)
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
        AnimatedPet(sprite: sprite, status: companion.status, since: companion.statusSince)
            // Away from bud: greyed and faded, so a stale pet never looks like a working one.
            .saturation(connected ? 1 : 0)
            .opacity(connected ? 1 : 0.55)
            .overlay(alignment: .topTrailing) {
                if companion.status == .needsYou && connected {
                    Circle().fill(.orange)
                        .frame(width: 12, height: 12)
                        .overlay(Circle().strokeBorder(.white.opacity(0.9), lineWidth: 1.5))
                        .padding(10)
                        .transition(.scale.combined(with: .opacity))
                }
            }
            .shadow(color: .black.opacity(0.22), radius: 5, y: 3)
            .contentShape(Rectangle())
            .animation(.easeOut(duration: 0.2), value: connected)
    }

    /// Fallback when no pet art is installed.
    private var paw: some View {
            Image(systemName: "pawprint.fill")
                .font(.system(size: 24, weight: .medium))
                .foregroundStyle(.primary)
                .frame(width: 56, height: 56)
                .surface(Circle(), material: .hudWindow)
                .overlay(StatusRing(status: companion.status, link: companion.link))
                .shadow(color: .black.opacity(0.18), radius: 6, y: 2)
                .contentShape(Circle())
    }

    private var statusText: String {
        if case .problem(let detail) = companion.link { return detail }
        return companion.status.label
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
                .surface(Circle(), material: .hudWindow)
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
        VStack(alignment: .leading, spacing: 8) {
            if let headline {
                Label(headline.text, systemImage: headline.symbol)
                    .font(.callout.weight(.medium))
                    .foregroundStyle(headline.tint)
            }
            ForEach(companion.notices, id: \.self) { notice in
                Text(notice)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            if !companion.reply.isEmpty || companion.status == .working {
                // The bubble hugs its text up to `maxReply`; longer replies scroll, newest in view.
                // Measured, because a ScrollView (or ViewThatFits) takes every point it's offered.
                let overflows = companion.replyHeight > Self.maxReply
                ScrollView {
                    // Measured before the padding, so adding it can't flip `overflows` back.
                    reply
                        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
                            companion.replyHeight = $0
                        }
                        .padding(.bottom, overflows ? 12 : 0)
                }
                .scrollBounceBehavior(.basedOnSize)
                .defaultScrollAnchor(.bottom)
                .frame(height: min(max(companion.replyHeight, 1), Self.maxReply))
                // A soft edge, not a divider, says "there's more"; only when there is.
                .mask(LinearGradient(
                    stops: [.init(color: .black, location: overflows ? 0.85 : 1),
                            .init(color: overflows ? .clear : .black, location: 1)],
                    startPoint: .top, endPoint: .bottom))
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(width: 320, alignment: .leading)
        .surface(shape)
        .shadow(color: .black.opacity(0.16), radius: 12, y: 4)
        .overlay(alignment: .topTrailing) { close }
        .onHover { companion.bubbleHovered = $0 }
    }

    private static let maxReply: CGFloat = 220

    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: 14, style: .continuous) }

    private var reply: some View {
        Text(companion.reply.isEmpty ? AttributedString("Working…") : markdown(companion.reply))
            .font(.body)
            .lineSpacing(2)
            .foregroundStyle(companion.reply.isEmpty ? .secondary : .primary)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Shown only on hover: less chrome on a surface that floats over your work.
    private var close: some View {
        Button {
            companion.bubbleOpen = false
        } label: {
            Image(systemName: "xmark")
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(.secondary)
                .frame(width: 20, height: 20)
                .surface(Circle(), material: .hudWindow)
        }
        .buttonStyle(.plain)
        .padding(6)
        .opacity(companion.bubbleHovered ? 1 : 0)
        .animation(.easeOut(duration: 0.12), value: companion.bubbleHovered)
        .accessibilityLabel("Close reply")
    }

    /// A header only when it changes what you should do.
    private var headline: (text: String, symbol: String, tint: Color)? {
        if case .problem(let detail) = companion.link {
            return (detail, "bolt.horizontal.circle", .secondary)
        }
        switch companion.status {
        case .needsYou: return ("bud needs your decision", "hand.raised", .orange)
        case .failed: return ("Something went wrong", "exclamationmark.triangle", .red)
        default: return nil
        }
    }

    private func markdown(_ text: String) -> AttributedString {
        // Inline markdown only (block parsing drops line breaks in Text); list markers become bullets.
        let text = text
            .split(separator: "\n", omittingEmptySubsequences: false)
            .map { line -> String in
                let trimmed = line.drop(while: { $0 == " " })
                let indent = String(repeating: " ", count: line.count - trimmed.count)
                for marker in ["- ", "* ", "+ "] where trimmed.hasPrefix(marker) {
                    return indent + "• " + trimmed.dropFirst(2)
                }
                return String(line)
            }
            .joined(separator: "\n")
        let options = AttributedString.MarkdownParsingOptions(
            interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}

// MARK: Input

/// Opens without animation: you open it often, often from the keyboard.
private struct InputField: View {
    @Bindable var companion: Companion
    @FocusState private var focused: Bool

    var body: some View {
        TextField("Ask bud…", text: $companion.draft, axis: .vertical)
            .textFieldStyle(.plain)
            .font(.body)
            .lineLimit(1...5)
            .focused($focused)
            .onSubmit(companion.send)
            .onExitCommand { companion.inputOpen = false }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .frame(width: 320)
            .surface(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .shadow(color: .black.opacity(0.12), radius: 8, y: 2)
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
