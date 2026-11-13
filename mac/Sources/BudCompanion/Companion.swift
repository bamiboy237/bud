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
    /// What the pet shows: what bud is doing, unless a decision is waiting.
    private(set) var status: PetStatus = .idle {
        didSet { if status != oldValue { statusSince = .now } }
    }
    /// What bud is doing, from run events.
    @ObservationIgnored private var activity: PetStatus = .idle {
        didSet { showStatus() }
    }
    /// Approvals and questions still waiting on the owner, the main chat's and its jobs'.
    /// While any is open the pet shows "needs you", whatever else happens.
    @ObservationIgnored private var openApprovals: Set<String> = [] {
        didSet { showStatus() }
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
    @ObservationIgnored private var handshake: Task<Void, Never>?
    /// Bumped by each start(), so a slower earlier connection can't install itself afterwards.
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var approvalPoll: Task<Void, Never>?
    /// The newest stored event handled, so an older snapshot can't undo a newer event.
    @ObservationIgnored private var seenSeq = 0
    /// A message that failed to send, kept with its id so resending it can't run it twice.
    @ObservationIgnored private var unsent: (text: String, id: String)?
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
        generation += 1
        let mine = generation
        handshake?.cancel()
        follower?.cancel()
        retry?.cancel()
        approvalPoll?.cancel()
        link = .connecting
        handshake = Task {
            do {
                var connection = try BudConnection(settings: BudSettings.load())
                let runtime = try await connection.send("GET", "runtime")
                connection.scope = runtime["scope"] as? String ?? ""
                let primary = try await connection.send("POST", "sessions/primary", json: [:])
                guard let session = primary["session_id"] as? String else {
                    throw ConnectionError.http(0, "no session returned")
                }
                let snapshot = try await connection.send("GET", "sessions/\(session)")
                // A newer start() owns the companion now; this one bows out.
                guard mine == generation else { return }
                self.connection = connection
                self.session = session
                seenSeq = 0
                apply(snapshot: snapshot)
                let start = snapshot["last_seq"] as? Int ?? 0
                // The companion lives as long as the app, so a strong capture is fine.
                follower = Task {
                    await followEvents(connection, session: session, after: start) { signal in
                        Companion.shared.receive(signal, generation: mine)
                    }
                }
                retryDelay = .seconds(1)
            } catch ConnectionError.unreachable {
                guard mine == generation else { return }
                // bud isn't up yet (or is restarting): keep trying quietly, backing off to 15 s.
                link = .problem("\(ConnectionError.unreachable)")
                let delay = retryDelay
                retryDelay = min(retryDelay * 2, .seconds(15))
                retry = Task {
                    try? await Task.sleep(for: delay)
                    if !Task.isCancelled { start() }
                }
            } catch {
                guard mine == generation else { return }
                // A bad token or URL: retrying can't help, so wait for Settings or Reconnect.
                link = .problem("\(error)")
            }
        }
    }

    private func apply(snapshot: [String: Any]) {
        switch snapshot["status"] as? String {
        case "running", "queued", "stopping": activity = .working
        default: activity = .idle
        }
        setOpenApprovals(from: snapshot)
        let turns = snapshot["transcript"] as? [[String: Any]] ?? []
        // Only a reply to the latest message: after a newer message, an older reply misleads.
        if let last = turns.last, last["role"] as? String == "assistant" {
            reply = last["text"] as? String ?? ""
        } else {
            reply = ""
        }
    }

    private func showStatus() {
        status = openApprovals.isEmpty ? activity : .needsYou
    }

    /// The snapshot's pending list is the authority. Events alone can't tell: a parked
    /// approval stays open, and a job's approval is decided in the job's own session.
    private func setOpenApprovals(from snapshot: [String: Any]) {
        let at = snapshot["last_seq"] as? Int ?? 0
        guard at >= seenSeq else { return }
        seenSeq = at
        let pending = snapshot["pending_approvals"] as? [[String: Any]] ?? []
        openApprovals = Set(pending.compactMap { $0["approval_id"] as? String })
        approvalPoll?.cancel()
        // A job approval answered in another interface sends nothing to this stream, so
        // check again while one is open.
        if !openApprovals.isEmpty {
            approvalPoll = Task {
                try? await Task.sleep(for: .seconds(10))
                if !Task.isCancelled { refreshApprovals() }
            }
        }
    }

    private func refreshApprovals() {
        guard let connection else { return }
        let mine = generation
        Task {
            guard let snapshot = try? await connection.send("GET", "sessions/\(session)"),
                  mine == generation
            else { return }
            setOpenApprovals(from: snapshot)
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
        // The pet starts working when bud says so (user.message), not on a hopeful send.
        sentAt = .now
        lastFirstText = nil
        let messageID = unsent?.text == text
            ? unsent!.id : UUID().uuidString.replacingOccurrences(of: "-", with: "")
        unsent = nil
        Task {
            do {
                // Built here: a [String: Any] can't cross into the task under Swift 6 checking.
                let body: [String: Any] = [
                    "text": text, "message_id": messageID, "origin": BudConnection.origin,
                ]
                _ = try await connection.send("POST", "sessions/\(session)/messages", json: body)
            } catch {
                // Keep what was typed: put it back in the field so one Return resends it. The
                // request may have arrived before the error, so the resend reuses its id and
                // bud runs it at most once.
                activity = .failed
                reply = "Couldn't send: \(error)"
                if draft.isEmpty {
                    draft = text
                    unsent = (text, messageID)
                }
                inputOpen = true
            }
        }
    }

    func stop() {
        guard let connection else { return }
        Task { _ = try? await connection.send("POST", "sessions/\(session)/stop", json: [:]) }
    }

    // MARK: Events

    private func receive(_ signal: StreamSignal, generation from: Int) {
        guard from == generation else { return }
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
        if !event.live { seenSeq = max(seenSeq, event.seq) }
        switch event.type {
        case "user.message":
            // A new turn, from this app or any other interface: the old reply no longer applies.
            settle?.cancel()
            blocks = []
            seenRequests = []
            reply = ""
            activity = .working
            bubbleOpen = true
        case "run.started", "run.recovered":
            settle?.cancel()
            activity = .working
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
            activity = .done
            settleToIdle()
            refreshApprovals()
        case "run.waiting":
            // The run is parked on the owner; the open approvals make the pet say so.
            finish(with: Self.replyText(event.data))
            activity = .idle
            bubbleOpen = true
        case "approval.requested", "task.approval":
            // Only the id: a job's approval must not touch the main chat's reply.
            let id = event.string("approval_id")
            if !id.isEmpty { openApprovals.insert(id) }
            bubbleOpen = true
            if event.type == "task.approval" { refreshApprovals() }
        case "approval.granted", "approval.denied", "approval.parked", "question.answered":
            refreshApprovals()
        case "run.failed":
            let reason = event.string("reason")
            reply = reason.isEmpty ? "The run failed." : "The run failed: \(reason)"
            activity = .failed
            bubbleOpen = true
            refreshApprovals()
        case "run.cancelled":
            activity = .idle
            refreshApprovals()
        case "task.created", "task.completed", "task.failed", "task.cancelled", "task.paused":
            notice(event)
            if event.type != "task.created" { refreshApprovals() }
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
            if !Task.isCancelled, activity == .done { activity = .idle }
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
