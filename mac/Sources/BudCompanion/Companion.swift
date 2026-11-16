import AppKit
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
enum InputPurpose: Hashable {
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

struct HeldMessage: Identifiable {
    let id: String
    let text: String
    let payload: String
    let session: String
    let base: URL
    let scope: String
}

struct RunningTask: Identifiable, Equatable, Sendable {
    let id: Int
    let title: String
    var status: String
    var finishedAt: Date? = nil

    var isWorking: Bool {
        status == "running" || status == "queued" || status == "waiting"
    }

    var move: PetMove {
        if status == "completed" || status == "done" {
            return .done
        } else if status == "failed" || status == "cancelled" {
            return .failed
        } else if status == "paused" {
            return .idle
        } else {
            return .working
        }
    }
}

/// One thing bud did on the way to its reply, in plain words.
struct Step: Identifiable, Equatable {
    /// The tool call it came from, and its place among that call's steps.
    let id: String
    let text: String
    var done = false
}

/// How the bubble shows bud's steps while it works.
enum StepsLayout: String, CaseIterable {
    /// The status beside bud's name names the current step.
    case line
    /// Every step stays as a row; finished ones are ticked.
    case checklist
    /// The current step, with a faint count of how many so far.
    case counted

    var title: String {
        switch self {
        case .line: "Live line"
        case .checklist: "Checklist"
        case .counted: "Line and count"
        }
    }
}

/// One message in the main conversation, yours or bud's.
struct Turn: Identifiable, Equatable {
    let id = UUID()
    let fromOwner: Bool
    let text: String
    /// What bud did for this reply, when it happened while the app was watching.
    var work: Work?
}

/// A run's work, for the folded line above its reply: "Worked for 34s · 4 steps".
struct Work: Equatable {
    var steps: [Step]
    var seconds: Int
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
        didSet {
            if status != oldValue {
                statusSince = .now
                reactionAtStatusStart = reaction
            }
        }
    }
    /// Whether a run is in progress. Stop and the live work views gate on this, not on
    /// `status`: a job's approval turns the pet to "needs you" while the main run still works.
    private(set) var running = false
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
    /// What bud has done so far in the current run, oldest first.
    private(set) var steps: [Step] = []
    /// The finished run's steps, for the summary above its reply (when that's turned on).
    private(set) var lastSteps: [Step] = []
    var stepsExpanded = false
    var stepsLayout = StepsLayout(rawValue: UserDefaults.standard.string(forKey: "stepsLayout") ?? "") ?? .line {
        didSet { UserDefaults.standard.set(stepsLayout.rawValue, forKey: "stepsLayout") }
    }
    /// Off by default: the reply is what matters, and the steps were the means to it.
    var showStepsAfter = UserDefaults.standard.bool(forKey: "showStepsAfter") {
        didSet { UserDefaults.standard.set(showStepsAfter, forKey: "showStepsAfter") }
    }
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
    /// The main conversation so far, finished turns only: the reply being written is `shown`.
    private(set) var turns: [Turn] = []
    /// What you last asked, shown faded above the reply so the answer keeps its context.
    private(set) var question = ""
    /// When the current run began, for "Working for 12s"; nil when bud isn't working.
    private(set) var runStart: Date?
    /// The last finished run's work, until its reply joins `turns`.
    private(set) var lastWork: Work?
    /// A reply (or part of one) you're answering; it's quoted at the top of your message.
    var replyingTo: String?
    /// While you hold the talk key: the pet waits, listening.
    var hearing = false {
        didSet { if hearing != oldValue { hearingSince = .now } }
    }
    @ObservationIgnored private var hearingSince = Date.now
    /// While the conversation window is open, it is where bud talks: the bubble stays shut.
    var windowOpen = false
    /// What's waiting on the owner, oldest first, with enough detail to decide in the bubble.
    private(set) var approvals: [Approval] = []
    private(set) var setups: [CapabilitySetup] = [] {
        didSet {
            showStatus()
            monitorSetups()
        }
    }
    @ObservationIgnored private var setupMonitor: Task<Void, Never>?
    private(set) var updatingSetups: Set<String> = []
    private(set) var setupProblems: [String: String] = [:]
    var needsAttention: Bool { !approvals.isEmpty || !setups.isEmpty }
    /// Decisions sent and not yet confirmed: their buttons stay disabled, so one click is one call.
    private(set) var deciding: Set<String> = []
    /// True while a batch approval is running, so single decisions wait for it.
    private(set) var approvingBatch = false
    /// Why the last decision didn't go through, shown on the card.
    private(set) var decisionProblem: String?
    private(set) var approvalFailures: [String: String] = [:]
    /// True while any pending item is a question rather than a permission request.
    var hasQuestion: Bool { approvals.contains(where: \.isQuestion) }
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
    @ObservationIgnored private var savedDrafts: [InputPurpose: String] = [:]
    private(set) var heldMessages: [HeldMessage] = []
    private(set) var retryingHeldMessage = false
    /// Short job milestones (started, needs you, finished). Progress notes never come here.
    var notices: [String] = []
    /// Active background tasks/jobs and their helper pups (capped at max 4).
    private(set) var activeTasks: [RunningTask] = []
    var visiblePups: [RunningTask] { Array(activeTasks.prefix(4)) }
    /// Send to first streamed text, for the latency baseline.
    var lastFirstText: Duration?

    @ObservationIgnored private var connection: BudConnection?
    @ObservationIgnored private var session = ""
    @ObservationIgnored private var follower: Task<Void, Never>?
    @ObservationIgnored private var handshake: Task<Void, Never>?
    /// Bumped by each start(), so a slower earlier connection can't install itself afterwards.
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var approvalPoll: Task<Void, Never>?
    @ObservationIgnored private var batchTask: Task<Void, Never>?
    /// The newest stored event handled, so an older snapshot can't undo a newer event.
    @ObservationIgnored private var seenSeq = 0
    /// A message that failed to send, kept with its id so resending it can't run it twice.
    @ObservationIgnored private var unsent: HeldMessage?
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
    private(set) var walk: (move: PetMove, since: Date)?
    @ObservationIgnored private var lastPointerX: CGFloat?
    /// A short gesture the pet plays over its status (a wave, a hop), then drops.
    @ObservationIgnored private var reaction: (move: PetMove, since: Date)?
    @ObservationIgnored private var reactionAtStatusStart: (move: PetMove, since: Date)?
    @ObservationIgnored private nonisolated(unsafe) var activationObserver: (any NSObjectProtocol)?

    init() {
        activationObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didBecomeActiveNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                self?.refreshSetupStatus()
            }
        }
    }

    deinit {
        if let activationObserver {
            NotificationCenter.default.removeObserver(activationObserver)
        }
    }

    /// Connect (or reconnect after a settings change): handshake, primary session, snapshot, stream.
    func start() {
        generation += 1
        setupMonitor?.cancel()
        setupMonitor = nil
        fastPollRemaining = 0
        batchTask?.cancel()
        approvingBatch = false
        deciding.removeAll()
        approvalFailures = [:]
        setups = []
        updatingSetups = []
        setupProblems = [:]
        activeTasks = []
        connection = nil
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

    func apply(snapshot: [String: Any]) {
        switch snapshot["status"] as? String {
        case "running", "queued", "stopping":
            activity = .working
            // Joined mid-run: the timer counts from now, not from a start it never saw.
            if runStart == nil { runStart = .now }
        default: activity = .idle
        }
        setOpenApprovals(from: snapshot)
        // The main chat's turns; a job's messages live in the job, not here.
        let transcript = (snapshot["transcript"] as? [[String: Any]] ?? []).filter {
            $0["task_id"] == nil || $0["task_id"] is NSNull
        }
        turns = transcript.compactMap { turn in
            guard let text = turn["text"] as? String, !text.isEmpty else { return nil }
            return Turn(fromOwner: turn["role"] as? String == "user", text: text)
        }
        question = turns.last(where: \.fromOwner)?.text ?? ""
        // Only a reply to the latest message: after a newer message, an older reply misleads.
        // It becomes the reply, not a turn: the latest reply is always `reply`, so it's drawn once.
        if let last = turns.last, !last.fromOwner {
            reply = last.text
            turns.removeLast()
        } else {
            reply = ""
        }
        // An old reply isn't news: it appears whole.
        showWhole()
        // Something already waiting on you is shown, not left behind the dot.
        if needsAttention { bubbleOpen = true }
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
        running = activity == .working
        status = openApprovals.isEmpty && setups.isEmpty ? activity : .needsYou
    }

    /// The snapshot's pending list is the authority. Events alone can't tell: a parked
    /// approval stays open, and a job's approval is decided in the job's own session.
    private func setOpenApprovals(from snapshot: [String: Any]) {
        let at = snapshot["last_seq"] as? Int ?? 0
        guard at >= seenSeq else { return }
        seenSeq = at
        let previous = Dictionary(uniqueKeysWithValues: setups.map { ($0.id, $0) })
        setups = (snapshot["pending_setup"] as? [[String: Any]] ?? []).compactMap {
            guard var setup = CapabilitySetup($0),
                  !["connected", "cancelled"].contains(setup.status) else { return nil }
            setup.url = setup.url ?? previous[setup.id]?.url
            return setup
        }
        setupProblems = setupProblems.filter { key, _ in setups.contains { $0.id == key } }
        let pending = snapshot["pending_approvals"] as? [[String: Any]] ?? []
        var jobs: [Int: String] = [:]
        let rawTasks = snapshot["tasks"] as? [[String: Any]] ?? []
        for task in rawTasks {
            if let id = task["task_id"] as? Int { jobs[id] = task["title"] as? String }
        }
        let now = Date()
        activeTasks = rawTasks.compactMap { dict in
            guard let id = dict["task_id"] as? Int,
                  let status = dict["status"] as? String else { return nil }
            if let existing = activeTasks.first(where: { $0.id == id }) {
                if !["completed", "failed", "cancelled"].contains(status) {
                    var updated = existing
                    updated.status = status
                    return updated
                } else if let finishedAt = existing.finishedAt, now.timeIntervalSince(finishedAt) < 3.5 {
                    return existing
                }
                return nil
            }
            if ["completed", "failed", "cancelled"].contains(status) { return nil }
            let title = dict["title"] as? String ?? "Task \(id)"
            return RunningTask(id: id, title: title.isEmpty ? "Task \(id)" : title, status: status)
        }
        approvals = pending.compactMap { Approval($0, jobs: jobs) }
        closeStaleInput()
        if !Set(approvals.map(\.id)).isSubset(of: openApprovals) { arrivals += 1 }
        openApprovals = Set(approvals.map(\.id))
        questionFlows = questionFlows.filter { openApprovals.contains($0.key) }
        deciding.formIntersection(openApprovals)
        approvalFailures = approvalFailures.filter { openApprovals.contains($0.key) }
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

    func receiveSetup(_ data: [String: Any], replacing: Bool = false) {
        guard var incoming = CapabilitySetup(data) else { return }
        if ["connected", "cancelled"].contains(incoming.status) {
            setups.removeAll { $0.id == incoming.id }
            updatingSetups.remove(incoming.id)
            setupProblems.removeValue(forKey: incoming.id)
            return
        }
        if let index = setups.firstIndex(where: { $0.id == incoming.id }) {
            if !replacing {
                var current = setups[index]
                current.status = incoming.status
                current.reason = incoming.reason
                incoming = current
            }
            setups[index] = incoming
        } else {
            setups.append(incoming)
            arrivals += 1
        }
        bubbleOpen = true
    }

    @ObservationIgnored var fastPollRemaining = 0

    func connectSetup(_ setup: CapabilitySetup) {
        guard let connection, !updatingSetups.contains(setup.id) else { return }
        setupProblems.removeValue(forKey: setup.id)
        if let url = setups.first(where: { $0.id == setup.id })?.url {
            if !NSWorkspace.shared.open(url) {
                setupProblems[setup.id] = "Could not open your browser. Try again."
            } else {
                triggerFastPolling()
            }
            return
        }
        updatingSetups.insert(setup.id)
        let mine = generation
        Task {
            do {
                let result = try await connection.send(
                    "POST", "connections/\(setup.id)/connect", json: ["resume": true])
                guard mine == generation else { return }
                guard let url = CapabilitySetup.signInURL(result["url"] as? String) else {
                    setupProblems[setup.id] = "The sign-in link is unavailable. Try again."
                    updatingSetups.remove(setup.id)
                    return
                }
                if let index = setups.firstIndex(where: { $0.id == setup.id }) {
                    setups[index].url = url
                    if !NSWorkspace.shared.open(url) {
                        setupProblems[setup.id] = "Could not open your browser. Try again."
                    } else {
                        triggerFastPolling()
                    }
                }
            } catch {
                guard mine == generation else { return }
                setupProblems[setup.id] = "\(error)"
            }
            updatingSetups.remove(setup.id)
        }
    }

    func cancelSetup(_ setup: CapabilitySetup) {
        guard let connection, !updatingSetups.contains(setup.id) else { return }
        updatingSetups.insert(setup.id)
        setupProblems.removeValue(forKey: setup.id)
        let mine = generation
        let route = "sessions/\(session)/setup/\(setup.id)/cancel"
        Task {
            do {
                let result = try await connection.send("POST", route)
                guard mine == generation else { return }
                receiveSetup(result)
            } catch {
                guard mine == generation else { return }
                setupProblems[setup.id] = "\(error)"
            }
            updatingSetups.remove(setup.id)
        }
    }

    func triggerFastPolling() {
        fastPollRemaining = 3
        refreshSetupStatus()
    }

    func refreshSetupStatus() {
        setupMonitor?.cancel()
        setupMonitor = nil
        monitorSetups()
    }

    private func monitorSetups() {
        if setups.isEmpty {
            setupMonitor?.cancel()
            setupMonitor = nil
            fastPollRemaining = 0
            return
        }
        guard setupMonitor == nil, let connection else { return }
        let mine = generation
        setupMonitor = Task {
            while !Task.isCancelled, mine == generation, !setups.isEmpty {
                if link == .connected {
                    for setup in setups {
                        await refreshSetup(setup, connection: connection, generation: mine)
                    }
                }
                let delay: Duration
                if fastPollRemaining > 1 {
                    delay = .seconds(1)
                    fastPollRemaining -= 1
                } else if fastPollRemaining == 1 {
                    delay = .seconds(2)
                    fastPollRemaining = 0
                } else {
                    delay = .seconds(5)
                }
                do {
                    try await Task.sleep(for: delay)
                } catch {
                    break
                }
            }
        }
    }

    private func refreshSetup(
        _ setup: CapabilitySetup, connection: BudConnection, generation mine: Int
    ) async {
        guard !Task.isCancelled, setups.contains(where: { $0.id == setup.id }) else { return }
        do {
            let result = try await connection.send(
                "POST", "sessions/\(session)/setup/\(setup.id)/check")
            guard mine == generation, !Task.isCancelled, setups.contains(where: { $0.id == setup.id }) else { return }
            receiveSetup(result)
            setupProblems[setup.id] = result["status"] as? String == "failed"
                ? result["reason"] as? String : nil
        } catch {
            guard mine == generation, !Task.isCancelled, setups.contains(where: { $0.id == setup.id }) else { return }
            setupProblems[setup.id] = "\(error)"
        }
    }

    /// Sends what's in the field, or `spoken` words. Spoken words sent as a message leave the
    /// field as it was; spoken words answering bud's question are added to the field instead,
    /// for you to send with Return.
    /// `messageID` resends a held recording under its first id, so it can't arrive twice.
    func send(spoken: String? = nil, messageID heldID: String? = nil) {
        let text = (spoken ?? draft).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        guard let connection else {
            // Never lose what was said: it waits apart from the field, ready to resend.
            if spoken != nil { holdSpeech(text, because: "bud isn't connected, so that wasn't sent.") }
            return
        }
        if spoken == nil, inputPurpose == .ask, let unsent, unsent.text == text,
           (unsent.session != session || unsent.base != connection.base || unsent.scope != connection.scope) {
            nudge("Reconnect to the original bud session to retry this message.")
            return
        }
        // An answer to bud's question is yours to confirm: spoken words join the field, after
        // anything already typed, and Return sends them like a typed answer.
        if spoken != nil, inputPurpose != .ask {
            draft = draft.isEmpty ? text : draft + " " + text
            inputOpen = true
            return
        }
        switch inputPurpose {
        case .answer(let id):
            if let approval = approvals.first(where: { $0.id == id }), approval.isQuestionList {
                answerQuestionText(text, for: approval)
            } else {
                respond(to: id, path: "answer", body: ["text": text])
            }
            return
        case .revise(let id):
            respond(to: id, path: "revise", body: [
                "text": text, "message_id": UUID().uuidString.replacingOccurrences(of: "-", with: ""),
            ])
            return
        case .ask:
            break
        }
        if spoken == nil {
            draft = ""
            inputOpen = false
        }
        // The old reply moves into the conversation now, so the bubble clears at once.
        archiveReply()
        reply = ""
        question = text
        // A reply to something bud said carries it as a markdown quote, so bud sees what you mean.
        let message = spoken == nil && unsent?.text == text
            ? unsent!.payload : replyingTo.map { Self.quote($0) + "\n\n" + text } ?? text
        replyingTo = nil
        blocks = []
        bubbleOpen = true
        // The pet starts working when bud says so (user.message), not on a hopeful send.
        sentAt = .now
        lastFirstText = nil
        let messageID = heldID ?? (spoken == nil && unsent?.text == text
            ? unsent!.id : UUID().uuidString.replacingOccurrences(of: "-", with: ""))
        let destination = session
        // A typed message waiting for its retry keeps its id; speech never clears it.
        if spoken == nil { unsent = nil }
        Task {
            do {
                // Built here: a [String: Any] can't cross into the task under Swift 6 checking.
                let body: [String: Any] = [
                    "text": message, "message_id": messageID, "origin": BudConnection.origin,
                ]
                _ = try await connection.send("POST", "sessions/\(destination)/messages", json: body)
            } catch {
                // Keep what was typed: put it back in the field so one Return resends it. The
                // request may have arrived before the error, so the resend reuses its id and
                // bud runs it at most once.
                nudge("Couldn't send: \(error)")
                if spoken != nil {
                    holdSpeech(text, id: messageID, because: "That didn't send.")
                } else if draft.isEmpty, inputPurpose == .ask, session == destination,
                          self.connection?.base == connection.base,
                          self.connection?.scope == connection.scope {
                    draft = text
                    unsent = HeldMessage(
                        id: messageID, text: text, payload: message, session: destination,
                        base: connection.base, scope: connection.scope)
                    inputOpen = true
                } else {
                    heldMessages.append(HeldMessage(
                        id: messageID, text: text, payload: message, session: destination,
                        base: connection.base, scope: connection.scope))
                }
            }
        }
    }

    func retryHeldMessage() {
        guard let connection, let held = heldMessages.first, !retryingHeldMessage else { return }
        guard held.session == session, held.base == connection.base, held.scope == connection.scope else {
            nudge("Reconnect to the original bud session to retry this message.")
            return
        }
        retryingHeldMessage = true
        Task {
            do {
                _ = try await connection.send("POST", "sessions/\(held.session)/messages", json: [
                    "text": held.payload, "message_id": held.id, "origin": BudConnection.origin,
                ])
                heldMessages.removeAll { $0.id == held.id }
            } catch {
                nudge("Couldn't send: \(error)")
            }
            retryingHeldMessage = false
        }
    }

    func dropHeldMessage() {
        guard !heldMessages.isEmpty, !retryingHeldMessage else { return }
        heldMessages.removeFirst()
    }

    /// Spoken words that couldn't go out, held apart from the field, which keeps what you typed.
    private(set) var unsentSpeech: String?
    /// The id a held recording was first sent under, when it got that far.
    @ObservationIgnored private var unsentSpeechID: String?

    /// Spoken words: sent at once as a message, or added to the field when they answer a
    /// question of bud's.
    /// `destination` is what the field was for when you started talking. If a question was
    /// settled, or one arrived, while you spoke, the words are held rather than sent somewhere
    /// you didn't mean.
    func say(_ text: String, meant destination: InputPurpose) {
        guard destination == inputPurpose else {
            holdSpeech(text, because: "Things changed while you were talking, so that wasn't sent.")
            return
        }
        send(spoken: text)
    }

    /// The held line's button: sends the words as a message, or, while bud waits for an
    /// answer, adds them to the field (the button then says so).
    func resendSpeech() {
        guard let text = unsentSpeech else { return }
        let id = unsentSpeechID
        unsentSpeech = nil
        unsentSpeechID = nil
        send(spoken: text, messageID: id)
    }

    func dropSpeech() {
        unsentSpeech = nil
        unsentSpeechID = nil
    }

    private func holdSpeech(_ text: String, id: String? = nil, because reason: String) {
        unsentSpeech = text
        unsentSpeechID = id
        nudge(reason)
    }

    /// A short line in the bubble, for things like "voice isn't set up yet". Cleared with the
    /// next message, like job notices.
    func nudge(_ text: String) {
        if !notices.contains(text) { notices = Array((notices + [text]).suffix(3)) }
        bubbleOpen = true
    }

    func stop() {
        batchTask?.cancel()
        guard let connection else { return }
        Task { _ = try? await connection.send("POST", "sessions/\(session)/stop", json: [:]) }
    }

    // MARK: The pet's body language

    /// What the pet is doing, as facts; the sprite turns them into a frame, since only it
    /// knows how long each animation lasts.
    var pose: PetPose {
        PetPose(
            status: PetMove.of(status), statusSince: statusSince, walk: walk,
            listeningSince: hearing ? hearingSince : nil, reaction: reaction,
            reactionAtStatusStart: reactionAtStatusStart)
    }

    /// Plays `move` once over the status, unless the pet is busy being carried.
    func react(_ move: PetMove) {
        guard walk == nil else { return }
        reaction = (move, .now)
    }

    /// A wave when you point at the pet, but only when it has nothing else to show.
    func greet() {
        guard status == .idle || status == .done else { return }
        if let reaction, Date.now < reaction.since + 2 { return }
        react(.wave)
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
        let move: PetMove = dx > 0 ? .walkRight : .walkLeft
        if walk?.move != move { walk = (move, .now) }
    }

    func setDown() {
        walk = nil
        lastPointerX = nil
    }

    /// Yes or no on an action. A job's approval goes through this session too: bud finds the job.
    func decide(_ id: String, approved: Bool) {
        respond(to: id, path: nil, body: ApprovalDecision(approved: approved).body)
    }

    /// Yes now, and yes to this tool from now on: bud records the standing grant, then applies.
    func alwaysAllow(_ approval: Approval) {
        respond(to: approval.id, path: nil, body: ApprovalDecision.always.body)
    }

    func approveAll(for approval: Approval) {
        let batch = approval.batch(in: approvals)
        guard batch.count > 1 else { return }
        approve(batch)
    }

    func failedApprovals(for approval: Approval) -> [Approval] {
        approval.retryBatch(in: approvals, failures: approvalFailures)
    }

    func retryFailed(for approval: Approval) {
        approve(failedApprovals(for: approval))
    }

    private func approve(_ batch: [Approval]) {
        guard let connection, !approvingBatch, deciding.isEmpty, !batch.isEmpty else { return }
        let ids = batch.map(\.id)
        let route = "sessions/\(session)/approvals/"
        let mine = generation
        approvingBatch = true
        deciding.formUnion(ids)
        decisionProblem = nil
        batchTask = Task {
            let result = await ApprovalBatch.approve(ids) { id in
                try Task.checkCancellation()
                guard mine == generation else { throw CancellationError() }
                return try await connection.send("POST", route + id, json: ["approved": true])
            }
            guard mine == generation else { return }
            completeBatch(result, ids: ids)
        }
    }

    func completeBatch(_ result: ApprovalBatch.Result, ids: [String]) {
        approvals.removeAll { result.approved.contains($0.id) }
        openApprovals.subtract(result.approved)
        deciding.subtract(ids)
        approvingBatch = false
        for id in ids { approvalFailures.removeValue(forKey: id) }
        for (id, failure) in result.failures where openApprovals.contains(id) {
            approvalFailures[id] = failure
        }
        showReceipt(text: "Approved \(result.approved.count) of \(ids.count) actions")
        refreshApprovals()
    }

    /// Opens the field to answer a question in your own words, or to say what should change.
    func write(_ purpose: InputPurpose) {
        savedDrafts[inputPurpose] = draft
        inputPurpose = purpose
        draft = savedDrafts.removeValue(forKey: purpose) ?? ""
        inputOpen = true
    }

    /// Choosing one of a question's options sends it as the answer, word for word.
    func choose(_ choice: String, for id: String) {
        respond(to: id, path: "answer", body: ["text": choice])
    }

    @ObservationIgnored private var questionFlows: [String: QuestionFlow] = [:]

    func questionFlow(for approval: Approval) -> QuestionFlow {
        if let flow = questionFlows[approval.id] { return flow }
        let id = approval.id
        let flow = QuestionFlow(questions: approval.questions) { [weak self] answers in
            self?.answerQuestions(id, answers)
        }
        questionFlows[id] = flow
        return flow
    }

    private func answerQuestions(_ id: String, _ answers: [QuestionAnswer]) {
        respond(to: id, path: "answer", body: ["answers": answers])
    }

    func answerQuestionText(_ text: String, for approval: Approval) {
        savedDrafts.removeValue(forKey: .answer(approval.id))
        draft = savedDrafts.removeValue(forKey: .ask) ?? ""
        inputOpen = !draft.isEmpty
        inputPurpose = .ask
        questionFlow(for: approval).answerTyped(text)
    }

    /// A field opened to answer or revise something that's since been settled (in another
    /// interface, say) has nothing left to reply to.
    private func closeStaleInput() {
        let target: String? = switch inputPurpose {
        case .answer(let id), .revise(let id): id
        case .ask: nil
        }
        guard let target, !approvals.contains(where: { $0.id == target }) else { return }
        let unfinished = draft
        inputPurpose = .ask
        let previous = savedDrafts.removeValue(forKey: .ask) ?? ""
        draft = [previous, unfinished].filter { !$0.isEmpty }.joined(separator: "\n\n")
        inputOpen = !draft.isEmpty
    }

    private func showReceipt(
        for approval: Approval, path: String?, approved: Bool?, remember: Bool, answer: String
    ) {
        let receipt: Receipt
        switch (path, approved) {
        case ("answer", _):
            receipt = Receipt(text: "Answered “\(answer.prefix(60))”", symbol: "checkmark")
        case ("revise", _):
            receipt = Receipt(text: "Sent back with your changes", symbol: "arrow.uturn.backward")
        case (_, true) where remember:
            receipt = Receipt(text: approval.alwaysAllowDone, symbol: "checkmark")
        case (_, true):
            receipt = Receipt(text: approval.done, symbol: "checkmark")
        default:
            receipt = Receipt(text: "Denied. Nothing was done", symbol: "xmark")
        }
        displayReceipt(receipt)
    }

    private func showReceipt(text: String) {
        displayReceipt(Receipt(text: text, symbol: "checkmark"))
    }

    private func displayReceipt(_ receipt: Receipt) {
        self.receipt = receipt
        if receipt.symbol == "checkmark" { react(.hop) }
        Task {
            try? await Task.sleep(for: .seconds(1.8))
            if self.receipt == receipt { self.receipt = nil }
        }
    }

    private func respond(to id: String, path: String?, body: [String: any Sendable]) {
        guard let connection, !approvingBatch, !deciding.contains(id) else { return }
        deciding.insert(id)
        decisionProblem = nil
        let route = "sessions/\(session)/approvals/\(id)" + (path.map { "/\($0)" } ?? "")
        // Typed in the field for this approval (an answer or a change), as opposed to a
        // button. A draft opened for another approval stays where it is.
        let targetsThis: Bool = switch inputPurpose {
        case .answer(let target), .revise(let target): target == id
        case .ask: false
        }
        let typed = targetsThis ? draft : ""
        if targetsThis {
            draft = savedDrafts.removeValue(forKey: .ask) ?? ""
            inputOpen = !draft.isEmpty
            inputPurpose = .ask
        }
        let approval = approvals.first { $0.id == id }
        let answer = (body["text"] as? String)
            ?? (body["answers"] as? [QuestionAnswer]).map(QuestionAnswer.summary)
            ?? typed
        let mine = generation
        Task {
            do {
                let response = try await connection.send("POST", route, json: Self.payload(body))
                guard mine == generation else { return }
                // Answers report `answered`; approvals and revisions report `applied`.
                try ApprovalBatch.requireApplied(
                    response, key: path == "answer" ? "answered" : "applied")
                if let approval {
                    showReceipt(for: approval, path: path, approved: body["approved"] as? Bool,
                                remember: body["remember"] as? Bool == true, answer: answer)
                }
                // Gone from the card at once; the snapshot confirms and brings the next one.
                approvals.removeAll { $0.id == id }
                openApprovals.remove(id)
                approvalFailures.removeValue(forKey: id)
            } catch {
                guard mine == generation else { return }
                if approvalFailures[id] != nil {
                    approvalFailures[id] = "\(error)"
                } else {
                    decisionProblem = "That didn't go through: \(error)"
                }
                if !typed.isEmpty {
                    // Keep what was typed, so trying again is one Return.
                    savedDrafts[inputPurpose] = draft
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
            // The previous reply joins the conversation, and the new message follows it.
            archiveReply()
            let said = event.string("text")
            question = Self.unquoted(said).text
            if !said.isEmpty { turns.append(Turn(fromOwner: true, text: said)) }
            runStart = .now
            blocks = []
            seenRequests = []
            reply = ""
            // Job milestones belong to the turn they happened in.
            notices = []
            steps = []
            lastSteps = []
            stepsExpanded = false
            activity = .working
            bubbleOpen = true
        case "run.started", "run.recovered":
            settle?.cancel()
            if runStart == nil { runStart = .now }
            activity = .working
        case "text.reset":
            let request = event.string("request")
            if !seenRequests.insert(request).inserted {
                blocks.removeAll { $0.id.hasPrefix(request + ":") }
                reply = blocks.map(\.text).joined(separator: "\n\n")
            }
        case "tool.call":
            addSteps(call: event.string("call_id"), tool: event.string("tool"),
                     args: event.data["args"] as? [String: Any] ?? [:])
        case "tool.result":
            let call = event.string("call_id") + ":"
            for index in steps.indices where steps[index].id.hasPrefix(call) { steps[index].done = true }
        case "text.delta":
            appendDelta(block: event.string("block"), text: event.string("text"))
        case "capability.setup", "capability.status":
            receiveSetup(event.data, replacing: event.type == "capability.setup")
        case "run.completed":
            finish(with: Self.replyText(event.data))
            closeSteps()
            react(.hop)
            activity = .done
            settleToIdle()
            refreshApprovals()
        case "run.waiting":
            // The run is parked on the owner; the open approvals make the pet say so.
            finish(with: Self.replyText(event.data))
            closeSteps()
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
                    approved: event.type == "approval.granted", remember: false,
                    answer: event.string("text"))
                approvals.removeAll { $0.id == id }
                openApprovals.remove(id)
                closeStaleInput()
            }
            refreshApprovals()
        case "run.failed":
            let reason = event.string("reason")
            closeSteps()
            reply = reason.isEmpty ? "The run failed." : "The run failed: \(reason)"
            showWhole()
            activity = .failed
            bubbleOpen = true
            refreshApprovals()
        case "run.cancelled":
            closeSteps()
            activity = .idle
            refreshApprovals()
        case "task.created":
            if let id = event.data["task_id"] as? Int {
                let title = event.string("title")
                if !activeTasks.contains(where: { $0.id == id }) {
                    activeTasks.append(RunningTask(id: id, title: title.isEmpty ? "Task \(id)" : title, status: "running"))
                }
            }
            notice(event)
        case "task.completed":
            if let id = event.data["task_id"] as? Int,
               let index = activeTasks.firstIndex(where: { $0.id == id }) {
                activeTasks[index].status = "completed"
                activeTasks[index].finishedAt = .now
                scheduleTaskRemoval(id: id, delay: 3.5)
            }
            notice(event)
            refreshApprovals()
        case "task.failed", "task.cancelled":
            if let id = event.data["task_id"] as? Int,
               let index = activeTasks.firstIndex(where: { $0.id == id }) {
                activeTasks[index].status = event.type == "task.failed" ? "failed" : "cancelled"
                activeTasks[index].finishedAt = .now
                scheduleTaskRemoval(id: id, delay: 2.5)
            } else if let id = event.data["task_id"] as? Int {
                activeTasks.removeAll { $0.id == id }
            }
            notice(event)
            refreshApprovals()
        case "task.paused":
            if let id = event.data["task_id"] as? Int,
               let index = activeTasks.firstIndex(where: { $0.id == id }) {
                activeTasks[index].status = "paused"
            }
            notice(event)
            refreshApprovals()
        default:
            break
        }
    }

    /// A tool call's steps join the list. A step that repeats the one before (bud reading a
    /// second email) doesn't add a row: the line already says it, and the run's end ticks it.
    private func addSteps(call: String, tool: String, args: [String: Any]) {
        for (index, text) in Activity.steps(tool: tool, args: args).enumerated() {
            if steps.last?.text == text { continue }
            steps.append(Step(id: "\(call):\(index)", text: text))
        }
    }

    /// The run is over: its steps all count as done, and they move to the summary.
    private func closeSteps() {
        if !steps.isEmpty {
            lastSteps = steps.map { Step(id: $0.id, text: $0.text, done: true) }
            steps = []
        }
        if let runStart {
            lastWork = Work(steps: lastSteps, seconds: Int(Date.now.timeIntervalSince(runStart).rounded()))
        }
        runStart = nil
    }

    /// The finished reply joins the conversation, carrying the work that produced it.
    private func archiveReply() {
        if !reply.isEmpty { turns.append(Turn(fromOwner: false, text: reply, work: lastWork)) }
        lastWork = nil
    }

    /// Quoted text as markdown, cut to a few hundred characters: enough to point at.
    static func quote(_ text: String) -> String {
        let cut = text.count > 400 ? String(text.prefix(400)) + "…" : text
        return cut.split(separator: "\n", omittingEmptySubsequences: false).map { "> " + $0 }.joined(separator: "\n")
    }

    /// A message split into the quote it opens with, if any, and your own words.
    static func unquoted(_ text: String) -> (quote: String?, text: String) {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        let quoted = lines.prefix { $0.hasPrefix(">") }
        guard !quoted.isEmpty else { return (nil, text) }
        let quote = quoted.map { $0.dropFirst().drop { $0 == " " } }.joined(separator: "\n")
        let rest = lines.dropFirst(quoted.count).joined(separator: "\n")
        return (quote, rest.trimmingCharacters(in: .whitespacesAndNewlines))
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

    private func scheduleTaskRemoval(id: Int, delay: Double) {
        Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(Int(delay * 1000)))
            await MainActor.run {
                self?.activeTasks.removeAll { $0.id == id }
            }
        }
    }

    func addTaskForTesting(id: Int, title: String = "Test job", status: String = "running") {
        if let idx = activeTasks.firstIndex(where: { $0.id == id }) {
            activeTasks[idx].status = status
            activeTasks[idx].finishedAt = nil
        } else {
            activeTasks.append(RunningTask(id: id, title: title, status: status))
        }
    }

    func completeTaskForTesting(id: Int) {
        if let idx = activeTasks.firstIndex(where: { $0.id == id }) {
            activeTasks[idx].status = "completed"
            activeTasks[idx].finishedAt = .now
        }
    }

    /// A decision's body, with the origin bud records for it. Built outside the main actor, so
    /// the dictionary is free to hand to the connection under Swift 6 checking.
    nonisolated private static func payload(_ body: [String: any Sendable]) -> [String: Any] {
        var json: [String: Any] = body.mapValues { $0 as Any }
        if let answers = body["answers"] as? [QuestionAnswer] {
            json["answers"] = answers.map(\.json)
        }
        json["origin"] = BudConnection.origin
        return json
    }

    /// The assistant's text from a stored run end: the server's rule (projection.reply_text),
    /// which leaves out bud's self-check notes, since they are for the model, not the owner.
    static func replyText(_ data: [String: Any]) -> String {
        let messages = data["messages"] as? [[String: Any]] ?? []
        return messages
            .filter { $0["kind"] as? String == "response" }
            .flatMap { message -> [String] in
                let spiral = (message["metadata"] as? [String: Any])?["bud_spiral"] as? [String: Any]
                let notes = Set(
                    (spiral?["notes"] as? [[String: Any]] ?? []).compactMap { $0["text"] as? String })
                return (message["parts"] as? [[String: Any]] ?? [])
                    .filter { $0["part_kind"] as? String == "text" }
                    .compactMap { $0["content"] as? String }
                    .filter { !notes.contains($0) }
            }
            .joined()
    }
}
