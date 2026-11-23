import CoreGraphics
import Foundation

// MARK: - Reconnect Backoff

/// Exponential reconnect schedule (`1s -> 2s -> 4s -> 8s -> 15s` cap) with bounded jitter.
/// Keeps reconnects calm during an outage instead of hammering `1s` forever, and resets on
/// a successful connection or received event.
struct ReconnectBackoff: Equatable, Sendable {
    static let defaultBaseSeconds: Double = 1.0
    static let defaultMaxSeconds: Double = 15.0
    static let defaultJitterFraction: Double = 0.15
    static let minSeconds: Double = 0.5

    let baseSeconds: Double
    let maxSeconds: Double
    let jitterFraction: Double

    private(set) var attemptCount: Int = 0
    private(set) var lastDelay: Duration? = nil

    init(
        baseSeconds: Double = ReconnectBackoff.defaultBaseSeconds,
        maxSeconds: Double = ReconnectBackoff.defaultMaxSeconds,
        jitterFraction: Double = ReconnectBackoff.defaultJitterFraction
    ) {
        self.baseSeconds = baseSeconds
        self.maxSeconds = maxSeconds
        self.jitterFraction = jitterFraction
    }

    /// Computes the delay for a 1-based `attempt` with `jitterUnit` in `[0, 1]`.
    /// `jitterUnit == 0.5` yields zero jitter (exact powers of two up to `maxSeconds`).
    func delay(forAttempt attempt: Int, jitterUnit: Double = 0.5) -> Duration {
        let exponent = max(0, attempt - 1)
        let uncapped = baseSeconds * pow(2.0, Double(exponent))
        let base = min(uncapped, maxSeconds)
        let clampedUnit = min(max(jitterUnit, 0.0), 1.0)
        let factor = 1.0 + ((clampedUnit * 2.0 - 1.0) * jitterFraction)
        let jittered = min(maxSeconds, max(Self.minSeconds, base * factor))
        return .milliseconds(Int((jittered * 1000.0).rounded()))
    }

    /// Advances `attemptCount` and returns the next jittered delay.
    mutating func nextDelay(jitterUnit: Double = Double.random(in: 0...1)) -> Duration {
        attemptCount += 1
        let computed = delay(forAttempt: attemptCount, jitterUnit: jitterUnit)
        lastDelay = computed
        return computed
    }

    mutating func reset() {
        attemptCount = 0
        lastDelay = nil
    }
}

// MARK: - Connection Machine

/// Pure, deterministic connection lifecycle state machine with monotonic `attempt` fencing.
/// Any async callback carrying a stale `attempt` ID is ignored so overlapping reconnects,
/// slow handshakes, or old retry timers can never overwrite newer connection state.
struct ConnectionMachine: Equatable, Sendable {
    enum Phase: Equatable, Sendable {
        case disconnected
        case connecting
        case connected
        case reconnecting(reason: String, delay: Duration)
        case problem(String)

        var label: String {
            switch self {
            case .disconnected: "disconnected"
            case .connecting: "connecting"
            case .connected: "connected"
            case .reconnecting: "reconnecting"
            case .problem: "problem"
            }
        }
    }

    enum Event: Equatable, Sendable {
        case start(resetBackoff: Bool = true)
        case handshakeSucceeded(attempt: Int)
        case handshakeFailed(attempt: Int, reason: String, retryable: Bool, jitterUnit: Double)
        case streamConnected(attempt: Int)
        case streamEventReceived(attempt: Int)
        case streamLost(attempt: Int, reason: String, jitterUnit: Double)
        case streamFatal(attempt: Int, reason: String)
        case retryTimerFired(attempt: Int)
        case disconnect
    }

    enum Effect: Equatable, Sendable {
        case cancelPending
        case beginHandshake(attempt: Int)
        case startStream(attempt: Int)
        case scheduleRetry(attempt: Int, delay: Duration)
    }

    private(set) var phase: Phase = .disconnected
    private(set) var attempt: Int = 0
    private(set) var backoff: ReconnectBackoff

    init(backoff: ReconnectBackoff = ReconnectBackoff()) {
        self.backoff = backoff
    }

    var backoffAttempt: Int { backoff.attemptCount }

    var link: Link {
        switch phase {
        case .disconnected:
            .problem("Disconnected")
        case .connecting:
            .connecting
        case .connected:
            .connected
        case .reconnecting(let reason, _), .problem(let reason):
            .problem(reason)
        }
    }

    @discardableResult
    mutating func reduce(_ event: Event) -> [Effect] {
        switch event {
        case .start(let resetBackoff):
            attempt += 1
            if resetBackoff { backoff.reset() }
            phase = .connecting
            return [.cancelPending, .beginHandshake(attempt: attempt)]

        case .handshakeSucceeded(let eventAttempt):
            guard eventAttempt == attempt else { return [] }
            return [.startStream(attempt: attempt)]

        case .handshakeFailed(let eventAttempt, let reason, let retryable, let jitterUnit):
            guard eventAttempt == attempt else { return [] }
            if retryable {
                let delay = backoff.nextDelay(jitterUnit: jitterUnit)
                phase = .reconnecting(reason: reason, delay: delay)
                return [.scheduleRetry(attempt: attempt, delay: delay)]
            } else {
                phase = .problem(reason)
                return [.cancelPending]
            }

        case .streamConnected(let eventAttempt):
            guard eventAttempt == attempt else { return [] }
            backoff.reset()
            phase = .connected
            return []

        case .streamEventReceived(let eventAttempt):
            guard eventAttempt == attempt else { return [] }
            backoff.reset()
            phase = .connected
            return []

        case .streamLost(let eventAttempt, let reason, let jitterUnit):
            guard eventAttempt == attempt else { return [] }
            let delay = backoff.nextDelay(jitterUnit: jitterUnit)
            phase = .reconnecting(reason: reason, delay: delay)
            return []

        case .streamFatal(let eventAttempt, let reason):
            guard eventAttempt == attempt else { return [] }
            phase = .problem(reason)
            return [.cancelPending]

        case .retryTimerFired(let eventAttempt):
            guard eventAttempt == attempt, case .reconnecting = phase else { return [] }
            attempt += 1
            phase = .connecting
            return [.cancelPending, .beginHandshake(attempt: attempt)]

        case .disconnect:
            attempt += 1
            backoff.reset()
            phase = .disconnected
            return [.cancelPending]
        }
    }
}

// MARK: - Scroll Follow Machine

/// Pure state machine for conversation pin-to-bottom tracking and `JumpButton` visibility.
/// Shows the jump pill only when content meaningfully overflows (`contentHeight > containerHeight + 80`)
/// and the viewport is scrolled up (`distanceFromBottom > 140`), and dismisses immediately on
/// `jumpToBottom()` with a `0.35s` post-jump guard against intermediate scroll animation geometry.
struct ScrollFollowMachine: Equatable, Sendable {
    static let scrollableSlack: CGFloat = 80
    static let bottomThreshold: CGFloat = 140
    static let jumpGuardDuration: TimeInterval = 0.35

    struct Metrics: Equatable, Sendable {
        let contentHeight: CGFloat
        let containerHeight: CGFloat
        let visibleMaxY: CGFloat

        var isScrollable: Bool {
            contentHeight > containerHeight + ScrollFollowMachine.scrollableSlack
        }

        var distanceFromBottom: CGFloat {
            contentHeight - visibleMaxY
        }

        var isNearBottom: Bool {
            !isScrollable || distanceFromBottom <= ScrollFollowMachine.bottomThreshold
        }
    }

    enum Event: Equatable, Sendable {
        case geometryChanged(Metrics, now: Date)
        case nearBottomChanged(Bool, now: Date)
        case jumpToBottom(now: Date)
        case reset
    }

    enum Effect: Equatable, Sendable {
        case scrollToEnd
    }

    private(set) var atBottom: Bool = true
    private(set) var jumpingUntil: Date = .distantPast

    var showsJumpButton: Bool { !atBottom }

    func isGuardActive(at now: Date) -> Bool {
        now < jumpingUntil
    }

    static func isNearBottom(
        contentHeight: CGFloat, containerHeight: CGFloat, visibleMaxY: CGFloat
    ) -> Bool {
        Metrics(
            contentHeight: contentHeight,
            containerHeight: containerHeight,
            visibleMaxY: visibleMaxY
        ).isNearBottom
    }

    @discardableResult
    mutating func reduce(_ event: Event) -> [Effect] {
        switch event {
        case .geometryChanged(let metrics, let now):
            apply(nearBottom: metrics.isNearBottom, now: now)
            return []
        case .nearBottomChanged(let nearBottom, let now):
            apply(nearBottom: nearBottom, now: now)
            return []
        case .jumpToBottom(let now):
            jumpingUntil = now.addingTimeInterval(Self.jumpGuardDuration)
            atBottom = true
            return [.scrollToEnd]
        case .reset:
            atBottom = true
            jumpingUntil = .distantPast
            return []
        }
    }

    private mutating func apply(nearBottom: Bool, now: Date) {
        if now < jumpingUntil {
            if !atBottom { atBottom = true }
        } else if atBottom != nearBottom {
            atBottom = nearBottom
        }
    }
}

// MARK: - Approval Deck Machine

/// Pure state machine for the approval card deck: index clamping on arrivals/removals,
/// single-click idempotency via in-flight `deciding` + monotonic attempt IDs, batch-approval
/// locking, and per-approval failure tracking.
struct ApprovalDeckMachine: Equatable, Sendable {
    private(set) var ids: [String] = []
    private(set) var currentIndex: Int = 0
    private(set) var deciding: Set<String> = []
    private(set) var approvingBatch: Bool = false
    private(set) var isAnimating: Bool = false
    private(set) var decisionProblem: String? = nil
    private(set) var failures: [String: String] = [:]
    private(set) var attemptCounter: Int = 0

    private var inFlightAttempts: [String: Int] = [:]
    private var batchAttempt: Int? = nil

    var count: Int { ids.count }
    var safeIndex: Int { ids.isEmpty ? 0 : min(max(currentIndex, 0), ids.count - 1) }
    var activeID: String? { ids.isEmpty ? nil : ids[safeIndex] }
    var canSelectPrevious: Bool { !isAnimating && safeIndex > 0 }
    var canSelectNext: Bool { !isAnimating && safeIndex < ids.count - 1 }

    func isBusy(_ id: String) -> Bool {
        deciding.contains(id) || approvingBatch
    }

    mutating func sync(ids newIDs: [String]) {
        ids = newIDs
        let openSet = Set(newIDs)
        deciding.formIntersection(openSet)
        inFlightAttempts = inFlightAttempts.filter { openSet.contains($0.key) }
        failures = failures.filter { openSet.contains($0.key) }
        if newIDs.isEmpty { decisionProblem = nil }
        clampIndex()
    }

    @discardableResult
    mutating func append(id: String) -> Bool {
        guard !ids.contains(id) else { return false }
        ids.append(id)
        clampIndex()
        return true
    }

    @discardableResult
    mutating func remove(id: String) -> Bool {
        guard let idx = ids.firstIndex(of: id) else { return false }
        ids.remove(at: idx)
        deciding.remove(id)
        inFlightAttempts.removeValue(forKey: id)
        failures.removeValue(forKey: id)
        if ids.isEmpty { decisionProblem = nil }
        clampIndex()
        return true
    }

    @discardableResult
    mutating func selectPrevious() -> Bool {
        guard canSelectPrevious else { return false }
        currentIndex = safeIndex - 1
        return true
    }

    @discardableResult
    mutating func selectNext() -> Bool {
        guard canSelectNext else { return false }
        currentIndex = safeIndex + 1
        return true
    }

    mutating func setAnimating(_ animating: Bool) {
        isAnimating = animating
        if !animating { clampIndex() }
    }

    /// Starts a single-approval decision if the deck is not batch-locked or already deciding `id`.
    /// Returns a monotonic attempt ID on acceptance, or `nil` when rejected.
    mutating func beginDecision(id: String) -> Int? {
        guard !approvingBatch, !deciding.contains(id) else { return nil }
        attemptCounter += 1
        let attempt = attemptCounter
        deciding.insert(id)
        inFlightAttempts[id] = attempt
        decisionProblem = nil
        return attempt
    }

    /// Completes an in-flight decision if `attempt` matches the active attempt for `id`.
    /// Passing `attempt: nil` bypasses attempt matching for synchronous unit tests.
    @discardableResult
    mutating func completeDecision(
        id: String, attempt: Int?, succeeded: Bool, errorMessage: String? = nil
    ) -> Bool {
        if let attempt {
            guard inFlightAttempts[id] == attempt else { return false }
        }
        inFlightAttempts.removeValue(forKey: id)
        deciding.remove(id)
        if succeeded {
            ids.removeAll { $0 == id }
            failures.removeValue(forKey: id)
            if ids.isEmpty { decisionProblem = nil }
            clampIndex()
        } else {
            let detail = errorMessage ?? "request failed"
            if failures[id] != nil {
                failures[id] = detail
            } else {
                decisionProblem = "That didn't go through: \(detail)"
            }
        }
        return true
    }

    /// Locks the deck for a batch approval when no individual or batch decision is in flight.
    mutating func beginBatch(ids batchIDs: [String]) -> Int? {
        guard !approvingBatch, deciding.isEmpty, !batchIDs.isEmpty else { return nil }
        attemptCounter += 1
        let attempt = attemptCounter
        approvingBatch = true
        batchAttempt = attempt
        deciding.formUnion(batchIDs)
        for id in batchIDs { inFlightAttempts[id] = attempt }
        decisionProblem = nil
        return attempt
    }

    /// Completes a batch approval if `attempt` matches (or `attempt == nil` in tests).
    @discardableResult
    mutating func completeBatch(
        attempt: Int?, ids batchIDs: [String], approved: Set<String>, failures newFailures: [String: String]
    ) -> Bool {
        if let attempt {
            guard batchAttempt == attempt else { return false }
        }
        batchAttempt = nil
        approvingBatch = false
        deciding.subtract(batchIDs)
        for id in batchIDs {
            inFlightAttempts.removeValue(forKey: id)
            failures.removeValue(forKey: id)
        }
        ids.removeAll { approved.contains($0) }
        let openSet = Set(ids)
        for (id, failure) in newFailures where openSet.contains(id) {
            failures[id] = failure
        }
        if ids.isEmpty { decisionProblem = nil }
        clampIndex()
        return true
    }

    mutating func reset() {
        attemptCounter += 1
        batchAttempt = nil
        inFlightAttempts.removeAll()
        deciding.removeAll()
        approvingBatch = false
        isAnimating = false
        decisionProblem = nil
        failures.removeAll()
        ids.removeAll()
        currentIndex = 0
    }

    private mutating func clampIndex() {
        currentIndex = safeIndex
    }
}
