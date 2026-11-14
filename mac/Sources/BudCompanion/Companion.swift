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

/// What the text you type is for: a new message, or a reply to something bud is waiting on.
enum InputPurpose: Equatable {
    case ask
    /// Your own answer to a question, when none of its options fit.
    case answer(String)
    /// Instead of approving an action: what bud should change before trying again.
    case revise(String)

    var prompt: String {
        switch self {
        case .ask: "Ask bud…"
        case .answer: "Your answer…"
        case .revise: "What should change?"
        }
    }
}

struct ReplyMore: Equatable {
    var above = false
    var below = false
}

/// What a decided card leaves behind for a moment, so a decision never vanishes unconfirmed.
struct Receipt: Equatable {
    let id = UUID()
    let text: String
    let symbol: String
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
    /// Counts new things needing you, so the pet can signal each arrival once.
    private(set) var arrivals = 0
    /// The line that stands in for a card once it's decided: "Sent", "Answered “Thursday”".
    private(set) var receipt: Receipt?
    /// Which pet to show; a missing pet falls back to the paw.
    let petID = UserDefaults.standard.string(forKey: "pet") ?? "dog"
    /// The reply as received so far. The bubble shows `shown`, which follows it at a reading
    /// pace: the stream arrives in bursts, and a burst dumped at once can't be read.
    var reply = "" {
        didSet { pace() }
    }
    private(set) var shown = ""
    /// What's waiting on the owner, oldest first, with enough detail to decide in the bubble.
    private(set) var approvals: [Approval] = []
    /// Decisions sent and not yet confirmed: their buttons stay disabled, so one click is one call.
    private(set) var deciding: Set<String> = []
    /// Why the last decision didn't go through, shown on the card.
    private(set) var decisionProblem: String?
    /// The approval whose full text is open on the card; others show a short preview.
    var expandedApproval: String?
    /// What the input field's text is for.
    var inputPurpose: InputPurpose = .ask
    /// Which side of the pet the bubble opens on, set by the panel as the pet is moved.
    var anchor = Anchor()
    var bubbleOpen = false
    var bubbleHovered = false
    /// The reply text's laid-out height, so the bubble can hug it.
    var replyHeight: CGFloat = 0
    /// Whether a scrolled reply has more text above or below what's visible.
    var replyMore: ReplyMore = .init()
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
    @ObservationIgnored private var pacer: Task<Void, Never>?
    /// True while the pet is being dragged, so letting go doesn't count as a click.
    @ObservationIgnored var dragging = false
    /// Which way the pet walks while you carry it; nil when it isn't moving.
    private(set) var walk: (row: PetRow, since: Date)?
    @ObservationIgnored private var lastPointerX: CGFloat?
    /// A short gesture the pet plays over its status (a wave, a hop), then drops.
    @ObservationIgnored private var reaction: (row: PetRow, since: Date)?

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
        // An old reply isn't news: it appears whole.
        showWhole()
        // Something already waiting on you is shown, not left behind the dot.
        if !approvals.isEmpty { bubbleOpen = true }
    }

    /// Moves `shown` toward `reply`. The step grows with the backlog, so a long burst eases in
    /// over about a second instead of landing at once, and a slow stream is shown as it comes.
    private func pace() {
        if !reply.hasPrefix(shown) {
            // The reply was replaced (a new turn, a replayed step): keep only what still holds.
            shown = String(reply.commonPrefix(with: shown))
        }
        guard pacer == nil, shown.count < reply.count else { return }
        pacer = Task {
            while !Task.isCancelled {
                let backlog = reply.count - shown.count
                if backlog <= 0 { break }
                shown = String(reply.prefix(shown.count + max(3, backlog / 30)))
                try? await Task.sleep(for: .milliseconds(25))
            }
            // A cancelled pacer was already replaced; only a finished one clears its slot.
            if !Task.isCancelled { pacer = nil }
        }
    }

    private func showWhole() {
        pacer?.cancel()
        pacer = nil
        shown = reply
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
        var jobs: [Int: String] = [:]
        for task in snapshot["tasks"] as? [[String: Any]] ?? [] {
            if let id = task["task_id"] as? Int { jobs[id] = task["title"] as? String }
        }
        approvals = pending.compactMap { Approval($0, jobs: jobs) }
        closeStaleInput()
        if !Set(approvals.map(\.id)).isSubset(of: openApprovals) { arrivals += 1 }
        openApprovals = Set(approvals.map(\.id))
        deciding.formIntersection(openApprovals)
        if approvals.isEmpty { decisionProblem = nil }
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
        switch inputPurpose {
        case .answer(let id):
            respond(to: id, path: "answer", body: ["text": text])
            return
        case .revise(let id):
            respond(to: id, path: "revise", body: [
                "text": text, "message_id": UUID().uuidString.replacingOccurrences(of: "-", with: ""),
            ])
            return
        case .ask:
            break
        }
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
                showWhole()
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

    // MARK: The pet's body language

    /// The row to play right now: carried, then a gesture in progress, then the status.
    func pose(at now: Date) -> (row: PetRow, since: Date) {
        if let walk { return walk }
        if let reaction, now < reaction.since + Double(reaction.row.loopMs) / 1000 {
            return reaction
        }
        return (PetRow.of(status), statusSince)
    }

    /// Plays `row` once over the status, unless the pet is busy being carried.
    func react(_ row: PetRow) {
        guard walk == nil else { return }
        reaction = (row, .now)
    }

    /// A wave when you point at the pet, but only when it has nothing else to show.
    func greet() {
        guard status == .idle || status == .done else { return }
        if let reaction, Date.now < reaction.since + 2 { return }
        react(.waving)
    }

    /// The pet walks the way it's being carried. Direction changes need a few points of
    /// movement, so a wobbly hand doesn't make it flip back and forth.
    func carry(pointerX: CGFloat) {
        guard let last = lastPointerX else {
            lastPointerX = pointerX
            return
        }
        let dx = pointerX - last
        guard abs(dx) >= 3 else { return }
        lastPointerX = pointerX
        let row: PetRow = dx > 0 ? .walkRight : .walkLeft
        if walk?.row.index != row.index { walk = (row, .now) }
    }

    func setDown() {
        walk = nil
        lastPointerX = nil
    }

    /// Yes or no on an action. A job's approval goes through this session too: bud finds the job.
    func decide(_ id: String, approved: Bool) {
        respond(to: id, path: nil, body: ["approved": approved])
    }

    /// Opens the field to answer a question in your own words, or to say what should change.
    func write(_ purpose: InputPurpose) {
        inputPurpose = purpose
        draft = ""
        inputOpen = true
    }

    /// Choosing one of a question's options sends it as the answer, word for word.
    func choose(_ choice: String, for id: String) {
        respond(to: id, path: "answer", body: ["text": choice])
    }

    /// A field opened to answer or revise something that's since been settled (in another
    /// interface, say) has nothing left to reply to.
    private func closeStaleInput() {
        let target: String? = switch inputPurpose {
        case .answer(let id), .revise(let id): id
        case .ask: nil
        }
        guard let target, !approvals.contains(where: { $0.id == target }) else { return }
        inputPurpose = .ask
        inputOpen = false
        draft = ""
    }

    private func showReceipt(for approval: Approval, path: String?, approved: Bool?, answer: String) {
        let receipt: Receipt
        switch (path, approved) {
        case ("answer", _):
            receipt = Receipt(text: "Answered “\(answer.prefix(60))”", symbol: "checkmark")
        case ("revise", _):
            receipt = Receipt(text: "Sent back with your changes", symbol: "arrow.uturn.backward")
        case (_, true):
            receipt = Receipt(text: approval.done, symbol: "checkmark")
        default:
            receipt = Receipt(text: "Denied. Nothing was done", symbol: "xmark")
        }
        self.receipt = receipt
        if receipt.symbol == "checkmark" { react(.jumping) }
        Task {
            try? await Task.sleep(for: .seconds(1.8))
            if self.receipt == receipt { self.receipt = nil }
        }
    }

    private func respond(to id: String, path: String?, body: [String: any Sendable]) {
        guard let connection, !deciding.contains(id) else { return }
        deciding.insert(id)
        decisionProblem = nil
        let route = "sessions/\(session)/approvals/\(id)" + (path.map { "/\($0)" } ?? "")
        // Typed in the field (an answer or a change), as opposed to a button or an option.
        let typed = inputPurpose == .ask ? "" : draft
        if inputPurpose != .ask {
            draft = ""
            inputOpen = false
            inputPurpose = .ask
        }
        let approval = approvals.first { $0.id == id }
        let answer = (body["text"] as? String) ?? typed
        Task {
            do {
                _ = try await connection.send("POST", route, json: Self.payload(body))
                if let approval {
                    showReceipt(for: approval, path: path, approved: body["approved"] as? Bool, answer: answer)
                }
                // Gone from the card at once; the snapshot confirms and brings the next one.
                approvals.removeAll { $0.id == id }
                openApprovals.remove(id)
            } catch {
                decisionProblem = "That didn't go through: \(error)"
                if !typed.isEmpty {
                    // Keep what was typed, so trying again is one Return.
                    inputPurpose = path == "answer" ? .answer(id) : .revise(id)
                    draft = typed
                    inputOpen = true
                }
            }
            deciding.remove(id)
            refreshApprovals()
        }
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
            // Job milestones belong to the turn they happened in.
            notices = []
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
            react(.jumping)
            settleToIdle()
            refreshApprovals()
        case "run.waiting":
            // The run is parked on the owner; the open approvals make the pet say so.
            finish(with: Self.replyText(event.data))
            activity = .idle
            bubbleOpen = true
        case "approval.requested", "task.approval":
            // Onto the card at once, from the event itself; the snapshot then confirms it. A
            // job's approval goes on the card too, but never touches the main chat's reply.
            let jobs = (event.data["task_id"] as? Int).map { [$0: event.string("title")] } ?? [:]
            if let approval = Approval(event.data, jobs: jobs),
               !approvals.contains(where: { $0.id == approval.id })
            {
                approvals.append(approval)
                openApprovals.insert(approval.id)
                arrivals += 1
            }
            bubbleOpen = true
            refreshApprovals()
        case "approval.granted", "approval.denied", "approval.parked", "question.answered":
            // Decided somewhere else (the terminal, another device): the card still closes
            // with a receipt. A decision made here is in `deciding`, and `respond` shows it.
            let id = event.string("approval_id")
            if event.type != "approval.parked", !deciding.contains(id),
               let approval = approvals.first(where: { $0.id == id })
            {
                showReceipt(
                    for: approval, path: event.type == "question.answered" ? "answer" : nil,
                    approved: event.type == "approval.granted", answer: event.string("text"))
                approvals.removeAll { $0.id == id }
                openApprovals.remove(id)
                closeStaleInput()
            }
            refreshApprovals()
        case "run.failed":
            let reason = event.string("reason")
            reply = reason.isEmpty ? "The run failed." : "The run failed: \(reason)"
            showWhole()
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
        // A job's own words when it sent some; otherwise what happened to it, by name.
        var text = event.string("text")
        let title = event.string("title")
        if text.isEmpty, !title.isEmpty {
            let happened = switch event.type {
            case "task.created": "Started"
            case "task.completed": "Finished"
            case "task.failed": "Couldn't finish"
            case "task.cancelled": "Stopped"
            case "task.paused": "Paused"
            default: "Job"
            }
            text = "\(happened) “\(title)”"
        }
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

    /// A decision's body, with the origin bud records for it. Built outside the main actor, so
    /// the dictionary is free to hand to the connection under Swift 6 checking.
    nonisolated private static func payload(_ body: [String: any Sendable]) -> [String: Any] {
        var json: [String: Any] = body.mapValues { $0 as Any }
        json["origin"] = BudConnection.origin
        return json
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
