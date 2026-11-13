import Foundation
import Observation
import os

/// What the pet shows. `needsYou` outranks everything: a waiting decision is never hidden.
enum PetStatus: String {
    case idle, working, needsYou, done, failed

    var label: String {
        switch self {
        case .idle: "Ready"
        case .working: "Working"
        case .needsYou: "Needs you"
        case .done: "Done"
        case .failed: "Something went wrong"
        }
    }
}

enum Link: Equatable {
    case connecting
    case connected
    case problem(String)
}

private let log = Logger(subsystem: "bud.companion", category: "session")

/// The companion's state, driven only by bud's own events: the pet never guesses.
@MainActor @Observable
final class Companion {
    static let shared = Companion()

    var link: Link = .connecting
    var status: PetStatus = .idle {
        didSet { if status != oldValue { statusSince = .now } }
    }
    /// When the status last changed: each state's animation starts at its first frame.
    var statusSince = Date.now
    /// Which pet to show; a missing pet falls back to the paw.
    let petID = UserDefaults.standard.string(forKey: "pet") ?? "dog"
    var reply = ""
    var bubbleOpen = false
    var bubbleHovered = false
    /// The reply text's laid-out height, so the bubble can hug it.
    var replyHeight: CGFloat = 0
    var inputOpen = false
    var draft = ""
    /// Short job milestones (started, needs you, finished). Progress notes never come here.
    var notices: [String] = []
    /// Send to first streamed text, for the latency baseline.
    var lastFirstText: Duration?

    @ObservationIgnored private var connection: BudConnection?
    @ObservationIgnored private var session = ""
    @ObservationIgnored private var follower: Task<Void, Never>?
    @ObservationIgnored private var blocks: [(id: String, text: String)] = []
    /// Model requests already streamed. A second text.reset for one means bud is replaying that
    /// step after a restart, so its partial text is dropped instead of shown twice.
    @ObservationIgnored private var seenRequests: Set<String> = []
    @ObservationIgnored private var retry: Task<Void, Never>?
    @ObservationIgnored private var retryDelay: Duration = .seconds(1)
    @ObservationIgnored private var sentAt: ContinuousClock.Instant?
    @ObservationIgnored private var settle: Task<Void, Never>?

    /// Connect (or reconnect after a settings change): handshake, primary session, snapshot, stream.
    func start() {
        follower?.cancel()
        retry?.cancel()
        link = .connecting
        Task {
            do {
                var connection = try BudConnection(settings: BudSettings.load())
                let runtime = try await connection.send("GET", "runtime")
                connection.scope = runtime["scope"] as? String ?? ""
                let primary = try await connection.send("POST", "sessions/primary", json: [:])
                guard let session = primary["session_id"] as? String else {
                    throw ConnectionError.http(0, "no session returned")
                }
                let snapshot = try await connection.send("GET", "sessions/\(session)")
                self.connection = connection
                self.session = session
                apply(snapshot: snapshot)
                let start = snapshot["last_seq"] as? Int ?? 0
                // The companion lives as long as the app, so a strong capture is fine.
                follower = Task {
                    await followEvents(connection, session: session, after: start) { signal in
                        Companion.shared.receive(signal)
                    }
                }
                retryDelay = .seconds(1)
            } catch ConnectionError.unreachable {
                // bud isn't up yet (or is restarting): keep trying quietly, backing off to 15 s.
                link = .problem("\(ConnectionError.unreachable)")
                let delay = retryDelay
                retryDelay = min(retryDelay * 2, .seconds(15))
                retry = Task {
                    try? await Task.sleep(for: delay)
                    if !Task.isCancelled { start() }
                }
            } catch {
                // A bad token or URL: retrying can't help, so wait for Settings or Reconnect.
                link = .problem("\(error)")
            }
        }
    }

    private func apply(snapshot: [String: Any]) {
        switch snapshot["status"] as? String {
        case "running", "queued", "stopping": status = .working
        case "awaiting_approval": status = .needsYou
        default: status = .idle
        }
        let turns = snapshot["transcript"] as? [[String: Any]] ?? []
        if let last = turns.last(where: { $0["role"] as? String == "assistant" }) {
            reply = last["text"] as? String ?? ""
        }
    }

    // MARK: Actions

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let connection else { return }
        draft = ""
        inputOpen = false
        reply = ""
        blocks = []
        bubbleOpen = true
        status = .working
        sentAt = .now
        lastFirstText = nil
        let messageID = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        Task {
            do {
                // Built here: a [String: Any] can't cross into the task under Swift 6 checking.
                let body: [String: Any] = [
                    "text": text, "message_id": messageID, "origin": BudConnection.origin,
                ]
                _ = try await connection.send("POST", "sessions/\(session)/messages", json: body)
            } catch {
                // Keep what was typed: put it back in the field so one Return resends it.
                status = .failed
                reply = "Couldn't send: \(error)"
                if draft.isEmpty { draft = text }
                inputOpen = true
            }
        }
    }

    func stop() {
        guard let connection else { return }
        Task { _ = try? await connection.send("POST", "sessions/\(session)/stop", json: [:]) }
    }

    // MARK: Events

    private func receive(_ signal: StreamSignal) {
        switch signal {
        case .connected:
            link = .connected
        case .lost:
            link = .problem("Lost bud. Reconnecting…")
        case .fatal(401):
            link = .problem("bud rejected the owner token.")
        case .fatal(409):
            link = .problem("bud switched accounts. Reconnect from the menu.")
        case .fatal(let code):
            link = .problem("bud closed the stream (\(code)).")
        case .event(let event):
            handle(event)
        }
    }

    private func handle(_ event: BudEvent) {
        switch event.type {
        case "user.message":
            // A new turn, from this app or any other interface: the old reply no longer applies.
            settle?.cancel()
            blocks = []
            seenRequests = []
            reply = ""
            status = .working
            bubbleOpen = true
        case "run.started", "run.recovered":
            settle?.cancel()
            status = .working
        case "text.reset":
            let request = event.string("request")
            if !seenRequests.insert(request).inserted {
                blocks.removeAll { $0.id.hasPrefix(request + ":") }
                reply = blocks.map(\.text).joined(separator: "\n\n")
            }
        case "text.delta":
            appendDelta(block: event.string("block"), text: event.string("text"))
        case "run.completed":
            finish(with: Self.replyText(event.data))
            status = .done
            settleToIdle()
        case "run.waiting", "approval.requested", "task.approval":
            finish(with: Self.replyText(event.data))
            status = .needsYou
            bubbleOpen = true
        case "run.failed":
            let reason = event.string("reason")
            reply = reason.isEmpty ? "The run failed." : "The run failed: \(reason)"
            status = .failed
            bubbleOpen = true
        case "run.cancelled":
            status = .idle
        case "task.created", "task.completed", "task.failed", "task.cancelled", "task.paused":
            notice(event)
        default:
            break
        }
    }

    private func appendDelta(block: String, text: String) {
        if let start = sentAt {
            lastFirstText = .now - start
            log.info("first text after \(String(describing: self.lastFirstText!), privacy: .public)")
            sentAt = nil
        }
        if let index = blocks.firstIndex(where: { $0.id == block }) {
            blocks[index].text += text
        } else {
            blocks.append((block, text))
        }
        reply = blocks.map(\.text).joined(separator: "\n\n")
        bubbleOpen = true
    }

    /// The stored run end is the authority: it replaces whatever was streamed.
    private func finish(with full: String) {
        blocks = []
        seenRequests = []
        if !full.isEmpty { reply = full }
    }

    private func notice(_ event: BudEvent) {
        let text = event.string("text").isEmpty ? event.string("title") : event.string("text")
        guard !text.isEmpty else { return }
        notices.append(String(text.prefix(160)))
        notices = Array(notices.suffix(3))
        bubbleOpen = true
    }

    private func settleToIdle() {
        settle?.cancel()
        settle = Task {
            try? await Task.sleep(for: .seconds(6))
            if !Task.isCancelled, status == .done { status = .idle }
        }
    }

    /// The assistant's text from a stored run end, the same rule the TUI uses.
    static func replyText(_ data: [String: Any]) -> String {
        let messages = data["messages"] as? [[String: Any]] ?? []
        return messages
            .filter { $0["kind"] as? String == "response" }
            .flatMap { $0["parts"] as? [[String: Any]] ?? [] }
            .filter { $0["part_kind"] as? String == "text" }
            .compactMap { $0["content"] as? String }
            .joined()
    }
}
