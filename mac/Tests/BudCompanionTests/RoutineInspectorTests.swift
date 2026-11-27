import Foundation
import Testing
@testable import BudCompanion

struct RoutineInspectorTests {
    @Test @MainActor
    func companionBumpsRoutineRevisionForAllRoutineEvents() throws {
        let companion = Companion()
        let initial = companion.routineRevision

        let cardEvents = [
            "routine.created",
            "routine.completed",
            "routine.failed",
            "routine.cancelled",
            "routine.needs_review",
        ]
        for (index, type) in cardEvents.enumerated() {
            let line = """
            {"seq":\(index + 1),"type":"\(type)","data":{"routine_id":"r1","occurrence_id":"o\(index)","title":"Monday briefing","text":"Routine briefing ready."}}
            """
            companion.handle(try #require(BudEvent(json: Substring(line))))
        }
        #expect(companion.routineRevision == initial + cardEvents.count)
        #expect(companion.turns.count == cardEvents.count)

        let silentEvents = [
            "routine.updated",
            "routine.paused",
            "routine.resumed",
            "routine.triggered",
            "routine.delivery_confirmed",
        ]
        for (index, type) in silentEvents.enumerated() {
            let line = """
            {"seq":\(10 + index),"type":"\(type)","data":{"routine_id":"r1","occurrence_id":"o1"}}
            """
            companion.handle(try #require(BudEvent(json: Substring(line))))
        }
        #expect(companion.routineRevision == initial + cardEvents.count + silentEvents.count)
        #expect(companion.turns.count == cardEvents.count)
    }

    @Test @MainActor
    func routineInspectorRefreshesRunningOccurrenceAfterCompletionEvent() async throws {
        let server = FakeRoutineServer(responses: [
            routinePayload(occurrences: []),
            routinePayload(occurrences: [
                occurrencePayload(id: "occ-1", execution: "running", delivery: "pending", reportPath: nil),
            ]),
            routinePayload(occurrences: [
                occurrencePayload(
                    id: "occ-1", execution: "completed", delivery: "confirmed",
                    reportPath: "reports/monday-briefing.md"),
            ]),
        ])
        let selection = RoutineSelection(
            connection: server.connection, session: "chat", routineID: "r1", occurrenceID: nil)
        let state = RoutineInspectorState()

        await state.start(with: selection)
        #expect(state.routine?.recentOccurrences.isEmpty == true)

        await state.change("r1", path: "/run-now", body: ["trigger_id": state.triggerID], using: selection)
        #expect(state.routine?.recentOccurrences.first?.executionStatus == "running")
        #expect(state.routine?.recentOccurrences.first?.deliveryStatus == "pending")

        state.requestRefresh(using: selection)
        await state.waitForRefresh()

        let finished = try #require(state.routine?.recentOccurrences.first)
        #expect(finished.executionStatus == "completed")
        #expect(finished.deliveryStatus == "confirmed")
        #expect(finished.reportPath == "reports/monday-briefing.md")
    }

    @Test @MainActor
    func routineEventArrivingDuringInFlightRunNowStillRefreshesAfterward() async throws {
        let state = RoutineInspectorState()
        let gate = AsyncGate()
        let server = FakeRoutineServer(
            responses: [
                routinePayload(occurrences: []),
                routinePayload(occurrences: [
                    occurrencePayload(id: "occ-1", execution: "running", delivery: "pending", reportPath: nil),
                ]),
                routinePayload(occurrences: [
                    occurrencePayload(
                        id: "occ-1", execution: "completed", delivery: "confirmed",
                        reportPath: "reports/monday-briefing.md"),
                ]),
            ],
            onDetailFetch: { fetchNumber in
                if fetchNumber == 2 {
                    await gate.arriveAndWait()
                }
            }
        )
        let selection = RoutineSelection(
            connection: server.connection, session: "chat", routineID: "r1", occurrenceID: nil)

        await state.start(with: selection)

        let runNowTask = Task {
            await state.change("r1", path: "/run-now", body: ["trigger_id": state.triggerID], using: selection)
        }
        await gate.waitUntilArrived()

        // Completion event arrives while change()'s GET /routines/r1 is still in flight.
        state.requestRefresh(using: selection)
        await gate.release()

        await runNowTask.value
        await state.waitForRefresh()

        let finished = try #require(state.routine?.recentOccurrences.first)
        #expect(finished.executionStatus == "completed")
        #expect(finished.deliveryStatus == "confirmed")
        #expect(finished.reportPath == "reports/monday-briefing.md")
    }

    @Test @MainActor
    func savingTitlePreservesConcurrentScheduleChanges() async throws {
        let refreshed = routinePayload(cron: "30 8 * * 2", timezone: "America/Chicago", occurrences: [])
        var saved = refreshed
        saved["title"] = "Weekly briefing"
        let server = FakeRoutineServer(responses: [
            routinePayload(cron: "0 9 * * mon", occurrences: []),
            refreshed,
            saved,
        ])
        let selection = RoutineSelection(
            connection: server.connection, session: "chat", routineID: "r1", occurrenceID: nil)
        let state = RoutineInspectorState()

        await state.start(with: selection)
        state.beginEditing(try #require(state.routine))
        state.title = "Weekly briefing"

        state.requestRefresh(using: selection)
        await state.waitForRefresh()
        #expect(state.routine?.cron == "30 8 * * 2")
        #expect(state.routine?.timezone == "America/Chicago")
        #expect(state.schedule.cron == "0 9 * * 1")
        #expect(state.timezone == "UTC")

        await state.save(using: selection)

        #expect(server.patchBodies == [["title": "Weekly briefing"]])
        #expect(state.routine?.title == "Weekly briefing")
        #expect(state.routine?.cron == "30 8 * * 2")
        #expect(state.routine?.timezone == "America/Chicago")
        #expect(!state.editing)
    }

    @Test(arguments: [true, false]) @MainActor
    func openingOlderReportPreservesCompletionRefresh(eventBeforeReport: Bool) async {
        let refreshGate = AsyncGate()
        let reportGate = AsyncGate()
        let olderRun = occurrencePayload(
            id: "occ-1", execution: "completed", delivery: "confirmed", reportPath: "reports/older.md")
        let running = routinePayload(occurrences: [
            occurrencePayload(id: "occ-2", execution: "running", delivery: "pending", reportPath: nil),
            olderRun,
        ])
        let completed = routinePayload(occurrences: [
            occurrencePayload(
                id: "occ-2", execution: "completed", delivery: "confirmed", reportPath: "reports/latest.md"),
            olderRun,
        ])
        let server = FakeRoutineServer(
            responses: eventBeforeReport ? [running, running, completed] : [running, completed],
            onDetailFetch: { fetchNumber in
                if eventBeforeReport && fetchNumber == 2 {
                    await refreshGate.arriveAndWait()
                }
            },
            onReportFetch: { await reportGate.arriveAndWait() }
        )
        let selection = RoutineSelection(
            connection: server.connection, session: "chat", routineID: "r1", occurrenceID: nil)
        let state = RoutineInspectorState()

        await state.start(with: selection)
        #expect(state.routine?.recentOccurrences.first?.executionStatus == "running")

        if eventBeforeReport {
            state.requestRefresh(using: selection)
            await refreshGate.waitUntilArrived()
        }
        let reportTask = Task { await state.openReport("r1", "occ-1", using: selection) }
        await reportGate.waitUntilArrived()
        if !eventBeforeReport {
            state.requestRefresh(using: selection)
        }
        await reportGate.release()
        await reportTask.value
        #expect(state.report == "Older run report")

        state.back(using: selection)
        await state.waitForRefresh()
        await refreshGate.release()

        #expect(state.report == nil)
        #expect(state.routine?.recentOccurrences.first?.id == "occ-2")
        #expect(state.routine?.recentOccurrences.first?.executionStatus == "completed")
    }

    @Test @MainActor
    func backgroundRefreshPreservesEditorDraftsAndRefreshesListView() async throws {
        let server = FakeRoutineServer(
            responses: [
                routinePayload(occurrences: [
                    occurrencePayload(id: "occ-1", execution: "running", delivery: "pending", reportPath: nil),
                ]),
                routinePayload(occurrences: [
                    occurrencePayload(id: "occ-1", execution: "completed", delivery: "confirmed", reportPath: nil),
                ]),
            ],
            listResponses: [
                ["routines": [routinePayload(status: "active", occurrences: [])]],
                ["routines": [routinePayload(status: "paused", occurrences: [])]],
            ]
        )
        let detailSelection = RoutineSelection(
            connection: server.connection, session: "chat", routineID: "r1", occurrenceID: nil)
        let detailState = RoutineInspectorState()

        await detailState.start(with: detailSelection)
        detailState.beginEditing(try #require(detailState.routine))
        detailState.title = "Unsaved title"
        detailState.prompt = "Unsaved plan"
        detailState.problem = "Keep validation error"

        detailState.requestRefresh(using: detailSelection)
        #expect(!detailState.pending)
        await detailState.waitForRefresh()

        #expect(detailState.editing)
        #expect(detailState.title == "Unsaved title")
        #expect(detailState.prompt == "Unsaved plan")
        #expect(detailState.problem == "Keep validation error")
        #expect(detailState.routine?.recentOccurrences.first?.executionStatus == "completed")

        let listSelection = RoutineSelection(
            connection: server.connection, session: "chat", routineID: nil, occurrenceID: nil)
        let listState = RoutineInspectorState()

        await listState.start(with: listSelection)
        #expect(listState.list.first?.status == "active")

        listState.requestRefresh(using: listSelection)
        await listState.waitForRefresh()
        #expect(listState.list.first?.status == "paused")
    }
}

private func routinePayload(
    status: String = "active",
    cron: String = "0 9 * * 1",
    timezone: String = "UTC",
    occurrences: [[String: Any]]
) -> [String: Any] {
    [
        "routine_id": "r1",
        "title": "Monday briefing",
        "skill": "general",
        "prompt": "Summarize the week",
        "cron": cron,
        "timezone": timezone,
        "status": status,
        "next_run_at": "2026-04-20T09:00:00Z",
        "recent_occurrences": occurrences,
    ]
}

private func occurrencePayload(
    id: String,
    execution: String,
    delivery: String,
    reportPath: String?
) -> [String: Any] {
    var payload: [String: Any] = [
        "occurrence_id": id,
        "scheduled_for": "2026-04-13T09:00:00Z",
        "execution_status": execution,
        "delivery_status": delivery,
    ]
    if let reportPath {
        payload["report_path"] = reportPath
    }
    return payload
}

private actor AsyncGate {
    private var arrived = false
    private var released = false
    private var arrivalWaiter: CheckedContinuation<Void, Never>?
    private var releaseWaiter: CheckedContinuation<Void, Never>?

    func arriveAndWait() async {
        arrived = true
        arrivalWaiter?.resume()
        arrivalWaiter = nil
        if !released {
            await withCheckedContinuation { releaseWaiter = $0 }
        }
    }

    func waitUntilArrived() async {
        if !arrived {
            await withCheckedContinuation { arrivalWaiter = $0 }
        }
    }

    func release() {
        released = true
        releaseWaiter?.resume()
        releaseWaiter = nil
    }
}

private final class FakeRoutineServer: @unchecked Sendable {
    private let lock = NSLock()
    private var detailResponses: [[String: Any]]
    private var listResponses: [[String: Any]]
    private var detailFetchCount = 0
    private var receivedPatches: [[String: String]] = []
    private let onDetailFetch: (@Sendable (Int) async -> Void)?
    private let onReportFetch: (@Sendable () async -> Void)?

    init(
        responses: [[String: Any]],
        listResponses: [[String: Any]] = [],
        onDetailFetch: (@Sendable (Int) async -> Void)? = nil,
        onReportFetch: (@Sendable () async -> Void)? = nil
    ) {
        self.detailResponses = responses
        self.listResponses = listResponses
        self.onDetailFetch = onDetailFetch
        self.onReportFetch = onReportFetch
    }

    var patchBodies: [[String: String]] {
        lock.withLock { receivedPatches }
    }

    var connection: BudConnection {
        BudConnection(
            base: URL(string: "http://127.0.0.1:8420")!,
            token: "test",
            transport: { request in
                try await self.handle(request)
            }
        )
    }

    private func handle(_ request: URLRequest) async throws -> (Data, URLResponse) {
        let url = try #require(request.url)
        let response = HTTPURLResponse(
            url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
        if request.httpMethod == "POST", url.path.hasSuffix("/run-now") {
            let data = try JSONSerialization.data(withJSONObject: ["occurrence_id": "occ-1"])
            return (data, response)
        }
        if request.httpMethod == "GET", url.path == "/routines" {
            let payload = nextListPayload()
            return (try JSONSerialization.data(withJSONObject: payload), response)
        }
        if request.httpMethod == "GET", url.path == "/routines/r1" {
            let (fetchNumber, payload) = nextDetailPayload()
            if let onDetailFetch {
                await onDetailFetch(fetchNumber)
            }
            return (try JSONSerialization.data(withJSONObject: payload), response)
        }
        if request.httpMethod == "PATCH", url.path == "/routines/r1" {
            let data = try #require(request.httpBody)
            let body = try #require(JSONSerialization.jsonObject(with: data) as? [String: String])
            lock.withLock { receivedPatches.append(body) }
            return (Data("{}".utf8), response)
        }
        if request.httpMethod == "GET", url.path == "/routines/r1/occurrences/occ-1/report" {
            await onReportFetch?()
            let data = try JSONSerialization.data(withJSONObject: ["content": "Older run report"])
            return (data, response)
        }
        throw ConnectionError.http(404, "unexpected request \(request.httpMethod ?? "") \(url.path)")
    }

    private func nextDetailPayload() -> (Int, [String: Any]) {
        lock.lock()
        defer { lock.unlock() }
        detailFetchCount += 1
        let index = min(detailFetchCount - 1, max(0, detailResponses.count - 1))
        return (detailFetchCount, detailResponses[index])
    }

    private func nextListPayload() -> [String: Any] {
        lock.lock()
        defer { lock.unlock() }
        if listResponses.count > 1 {
            return listResponses.removeFirst()
        }
        return listResponses.first ?? ["routines": []]
    }
}
