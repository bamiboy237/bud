import Foundation
import SwiftUI

struct RoutineCard: Equatable {
    let id: String
    let title: String
    let kind: String
    let occurrenceID: String?

    init?(_ value: Any?) {
        guard let data = value as? [String: Any], let id = data["routine_id"] as? String,
              let title = data["title"] as? String, let kind = data["kind"] as? String
        else { return nil }
        self.id = id
        self.title = title
        self.kind = kind
        occurrenceID = data["occurrence_id"] as? String
    }

    var label: String {
        switch kind {
        case "created": "Scheduled"
        case "completed": "Run completed"
        case "needs_review": "Needs review"
        case "cancelled": "Run stopped"
        case "failed": "Run failed"
        default: "Routine"
        }
    }
}

struct RoutineSelection: Identifiable {
    let connection: BudConnection
    let session: String
    let routineID: String?
    let occurrenceID: String?
    let id = UUID()
}

struct RoutineMessageCard: View {
    let card: RoutineCard
    let open: () -> Void

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Paper.radiusMD, style: .continuous)
        Button(action: open) {
            HStack(spacing: 10) {
                Image(systemName: "clock")
                    .font(.system(size: 13))
                    .foregroundStyle(Paper.inkSecondary)
                VStack(alignment: .leading, spacing: 3) {
                    Text(card.title)
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkPrimary)
                        .lineLimit(2)
                    Text(card.label)
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.inkMuted)
                }
                Spacer(minLength: 8)
                HStack(spacing: 4) {
                    Text("Open").font(Paper.Typography.captionMedium)
                    Image(systemName: "chevron.right").font(.system(size: 9))
                }
                .foregroundStyle(Paper.inkSecondary)
            }
            .padding(10)
            .frame(maxWidth: 360, alignment: .leading)
        }
        .buttonStyle(PaperHit(shape: shape, fill: Paper.elevated))
        .overlay { shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5).allowsHitTesting(false) }
        .accessibilityLabel("\(card.title), \(card.label)")
    }
}

struct RoutineSummary: Decodable, Equatable, Identifiable {
    let routineId: String
    let title: String
    let skill: String
    let prompt: String
    let cron: String
    let timezone: String
    let status: String
    let pauseReason: String?
    let nextRunAt: String?
    let recentOccurrences: [RoutineRun]
    var id: String { routineId }
}

struct RoutineRun: Decodable, Equatable, Identifiable {
    let occurrenceId: String
    let scheduledFor: String
    let executionStatus: String
    let deliveryStatus: String
    let reportPath: String?
    var id: String { occurrenceId }
}

@MainActor
final class RoutineInspectorState: ObservableObject {
    @Published var list: [RoutineSummary] = []
    @Published var routine: RoutineSummary?
    @Published var report: String?
    @Published var listing = true
    @Published var pending = true
    @Published var problem: String?
    @Published private(set) var editing = false
    @Published var title = ""
    @Published var prompt = ""
    @Published var schedule = RoutineSchedule(cron: "0 9 * * *")
    @Published var timezone = ""
    @Published var markedRun: String?
    @Published var triggerID = UUID().uuidString

    private var started = false
    private var editBaseline: [String: String]?
    private var activeRoutineID: String?
    private var viewGeneration = 0
    private var mutating = false
    private var refreshNeeded = false
    private var refreshTaskID = 0
    private var refreshTask: Task<Void, Never>?

    func start(with selection: RoutineSelection) async {
        started = true
        if let id = selection.routineID {
            markedRun = selection.occurrenceID
            await show(id, using: selection)
        } else {
            await loadList(using: selection)
        }
    }

    func requestRefresh(using selection: RoutineSelection) {
        guard started else { return }
        refreshNeeded = true
        guard !mutating, refreshTask == nil else { return }
        startRefreshTask(using: selection)
    }

    func cancelRefresh(preservingRequest: Bool = false) {
        refreshNeeded = preservingRequest && (refreshNeeded || refreshTask != nil)
        refreshTaskID += 1
        refreshTask?.cancel()
        refreshTask = nil
    }

    func waitForRefresh() async {
        while let task = refreshTask {
            await task.value
        }
    }

    func back(using selection: RoutineSelection) {
        if report != nil {
            report = nil
        } else {
            markedRun = nil
            listing = true
            endEditing()
            activeRoutineID = nil
            viewGeneration += 1
            Task { await loadList(using: selection) }
        }
    }

    func loadList(using selection: RoutineSelection) async {
        cancelRefresh()
        endEditing()
        viewGeneration += 1
        let generation = viewGeneration
        listing = true
        activeRoutineID = nil
        pending = true
        mutating = true
        problem = nil
        refreshNeeded = false
        do {
            let response = try await selection.connection.send(
                "GET", "routines", queryItems: [URLQueryItem(name: "feed_id", value: selection.session)])
            if generation == viewGeneration {
                list = try (response["routines"] as? [[String: Any]] ?? []).map(Self.decode)
            }
        } catch {
            if generation == viewGeneration { problem = "\(error)" }
        }
        pending = false
        mutating = false
        startRefreshTask(using: selection)
    }

    func show(_ id: String, using selection: RoutineSelection) async {
        cancelRefresh()
        endEditing()
        viewGeneration += 1
        let generation = viewGeneration
        listing = false
        activeRoutineID = id
        pending = true
        mutating = true
        problem = nil
        refreshNeeded = false
        do {
            let response = try await selection.connection.send("GET", "routines/\(id)")
            if generation == viewGeneration {
                routine = try Self.decode(response)
            }
        } catch {
            if generation == viewGeneration { problem = "\(error)" }
        }
        pending = false
        mutating = false
        startRefreshTask(using: selection)
    }

    func change(
        _ id: String,
        path: String = "",
        method: String = "POST",
        body: [String: String]? = nil,
        using selection: RoutineSelection
    ) async {
        guard !pending else { return }
        cancelRefresh()
        let generation = viewGeneration
        pending = true
        mutating = true
        problem = nil
        do {
            _ = try await selection.connection.send(method, "routines/\(id)\(path)", json: body)
            if path == "/run-now" { triggerID = UUID().uuidString }
            endEditing()
            refreshNeeded = false
            let response = try await selection.connection.send("GET", "routines/\(id)")
            if generation == viewGeneration {
                routine = try Self.decode(response)
            }
        } catch {
            if generation == viewGeneration { problem = "\(error)" }
        }
        pending = false
        mutating = false
        startRefreshTask(using: selection)
    }

    func beginEditing(_ value: RoutineSummary) {
        title = value.title
        prompt = value.prompt
        schedule = RoutineSchedule(cron: value.cron)
        timezone = value.timezone
        editBaseline = draftFields
        editing = true
    }

    func endEditing() {
        editBaseline = nil
        editing = false
    }

    func save(using selection: RoutineSelection) async {
        guard !pending, let editBaseline, let id = activeRoutineID else { return }
        let changes = draftFields.filter { editBaseline[$0.key] != $0.value }
        guard !changes.isEmpty else {
            endEditing()
            problem = nil
            return
        }
        await change(id, method: "PATCH", body: changes, using: selection)
    }

    private var draftFields: [String: String] {
        ["title": title, "prompt": prompt, "cron": schedule.cron, "timezone": timezone]
    }

    func openReport(_ routineID: String, _ occurrenceID: String, using selection: RoutineSelection) async {
        cancelRefresh(preservingRequest: true)
        let generation = viewGeneration
        pending = true
        mutating = true
        problem = nil
        do {
            let response = try await selection.connection.send(
                "GET", "routines/\(routineID)/occurrences/\(occurrenceID)/report")
            if generation == viewGeneration {
                report = response["content"] as? String
            }
        } catch {
            if generation == viewGeneration { problem = "\(error)" }
        }
        pending = false
        mutating = false
        startRefreshTask(using: selection)
    }

    private func startRefreshTask(using selection: RoutineSelection) {
        guard !mutating, refreshNeeded, refreshTask == nil else { return }
        refreshTaskID += 1
        let taskID = refreshTaskID
        refreshTask = Task { [weak self] in
            guard let self else { return }
            await self.runPendingRefreshes(using: selection, taskID: taskID)
        }
    }

    private func runPendingRefreshes(using selection: RoutineSelection, taskID: Int) async {
        defer {
            if refreshTaskID == taskID { refreshTask = nil }
        }
        while refreshNeeded, !mutating, !Task.isCancelled {
            refreshNeeded = false
            await performBackgroundRefresh(using: selection)
        }
    }

    private func performBackgroundRefresh(using selection: RoutineSelection) async {
        let generation = viewGeneration
        let targetRoutineID = listing ? nil : activeRoutineID
        do {
            if let id = targetRoutineID {
                let response = try await selection.connection.send("GET", "routines/\(id)")
                guard !Task.isCancelled, !mutating, generation == viewGeneration,
                      !listing, activeRoutineID == id
                else { return }
                routine = try Self.decode(response)
                if !editing { problem = nil }
            } else if listing {
                let response = try await selection.connection.send(
                    "GET", "routines", queryItems: [URLQueryItem(name: "feed_id", value: selection.session)])
                guard !Task.isCancelled, !mutating, generation == viewGeneration, listing else { return }
                list = try (response["routines"] as? [[String: Any]] ?? []).map(Self.decode)
                problem = nil
            }
        } catch {
            // Keep the last loaded routine or list visible if a background refresh fails transiently.
        }
    }

    private static func decode(_ data: [String: Any]) throws -> RoutineSummary {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(RoutineSummary.self, from: JSONSerialization.data(withJSONObject: data))
    }
}

@MainActor
struct RoutineInspector: View {
    let selection: RoutineSelection
    var refreshRevision = 0
    let close: () -> Void
    @StateObject private var state = RoutineInspectorState()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                if state.report != nil || !state.listing {
                    Button { state.back(using: selection) } label: { Image(systemName: "chevron.left") }
                        .buttonStyle(IconButton(size: 26))
                        .accessibilityLabel("Back")
                        .disabled(state.pending)
                }
                Text(heading)
                    .font(Paper.Typography.label)
                    .foregroundStyle(Paper.inkPrimary)
                    .lineLimit(1)
                Spacer()
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(IconButton(size: 26))
                    .accessibilityLabel("Close routines")
            }
            .font(.system(size: 12, weight: .medium))
            .padding(.horizontal, 14)
            .frame(height: 40)
            hairline
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if !state.editing, let problem = state.problem {
                        Text(problem).foregroundStyle(Paper.errorInk).textSelection(.enabled)
                    }
                    if let report = state.report {
                        MarkdownView(report, rich: true).textSelection(.enabled)
                    } else if state.listing {
                        if state.list.isEmpty, !state.pending, state.problem == nil {
                            Text("Ask Bud in chat to schedule a routine.")
                                .foregroundStyle(Paper.inkSecondary)
                        }
                        routineList
                    } else if let routine = state.routine {
                        detail(routine)
                    }
                    if state.pending { ProgressView().controlSize(.small) }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
            }
        }
        .foregroundStyle(Paper.inkPrimary)
        .background(Paper.canvas)
        .inspectorColumnWidth(min: 270, ideal: 340, max: 470)
        .task {
            await state.start(with: selection)
        }
        .onChange(of: refreshRevision) { _, _ in
            state.requestRefresh(using: selection)
        }
        .onDisappear {
            state.endEditing()
            state.cancelRefresh()
        }
    }

    private var heading: String {
        if state.report != nil { return "Run report" }
        if state.listing { return "Routines" }
        return state.editing ? "Edit routine" : "Routine"
    }

    private var hairline: some View {
        Rectangle().fill(Paper.hairline).frame(height: 0.5)
    }

    private var routineList: some View {
        VStack(spacing: 0) {
            ForEach(state.list) { item in
                Button {
                    Task { await state.show(item.id, using: selection) }
                } label: {
                    HStack(spacing: 8) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.title).font(Paper.Typography.callout)
                            Text(item.status.capitalized)
                                .font(Paper.Typography.caption)
                                .foregroundStyle(Paper.inkSecondary)
                        }
                        Spacer()
                        Image(systemName: "chevron.right")
                            .font(Paper.Typography.ledgerMeta)
                            .foregroundStyle(Paper.inkMuted)
                    }
                    .padding(9)
                }
                .buttonStyle(PaperHit(shape: RoundedRectangle(cornerRadius: Paper.radiusSM)))
                .disabled(state.pending)
                if item.id != state.list.last?.id { hairline }
            }
        }
    }

    @ViewBuilder
    private func detail(_ value: RoutineSummary) -> some View {
        if state.editing {
            editor
        } else {
            VStack(alignment: .leading, spacing: 4) {
                Text(value.title)
                    .font(Paper.Typography.h2)
                    .fixedSize(horizontal: false, vertical: true)
                Text(value.status.capitalized)
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.inkSecondary)
            }
            section("Plan") {
                let shape = RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
                let plan = Text(value.prompt)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                ViewThatFits(in: .vertical) {
                    plan
                    ScrollView { plan }.scrollBounceBehavior(.basedOnSize)
                }
                .font(Paper.Typography.callout)
                .textSelection(.enabled)
                .frame(maxHeight: 132)
                .padding(8)
                .background(Paper.elevated, in: shape)
                .overlay { shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5).allowsHitTesting(false) }
            }
            scheduleSummary(RoutineSchedule(cron: value.cron), timezone: value.timezone)
            if let next = value.nextRunAt {
                LabeledContent("Next run", value: date(next, timezone: TimeZone(identifier: value.timezone) ?? .current))
                    .font(Paper.Typography.callout)
            }
            if value.pauseReason == "uncertain" {
                Text("Review the last run before resuming. Bud could not confirm an action.")
                    .foregroundStyle(Paper.inkSecondary)
            }
            HStack(spacing: 6) {
                if value.status == "active" {
                    Button("Pause") { Task { await state.change(value.id, path: "/pause", using: selection) } }
                        .buttonStyle(CardButton(kind: .quiet))
                } else if value.status == "paused", value.pauseReason != "uncertain" {
                    Button("Resume") { Task { await state.change(value.id, path: "/resume", using: selection) } }
                        .buttonStyle(CardButton(kind: .primary))
                }
                Spacer(minLength: 0)
                Button("Edit") { state.beginEditing(value) }
                .buttonStyle(CardButton(kind: .secondary))
                if value.status == "active" {
                    Button("Run now") {
                        Task {
                            await state.change(
                                value.id, path: "/run-now", body: ["trigger_id": state.triggerID],
                                using: selection)
                        }
                    }
                    .buttonStyle(CardButton(kind: .primary))
                }
            }
            .disabled(state.pending)
            .opacity(state.pending ? 0.55 : 1)
        }
        if !state.editing && !value.recentOccurrences.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                Text("Run history")
                    .font(Paper.Typography.label)
                    .padding(.bottom, 6)
                ForEach(value.recentOccurrences) { run in
                    hairline
                    VStack(alignment: .leading, spacing: 4) {
                        Text(date(run.scheduledFor)).font(Paper.Typography.callout)
                        Text("\(label(run.executionStatus)) · \(label(run.deliveryStatus))")
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkSecondary)
                        if run.reportPath != nil {
                            Button("Open report") {
                                Task { await state.openReport(value.id, run.id, using: selection) }
                            }
                            .buttonStyle(.link)
                            .disabled(state.pending)
                        }
                    }
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(run.id == state.markedRun ? Paper.accentFill : .clear)
                }
            }
        }
    }

    private var editor: some View {
        VStack(alignment: .leading, spacing: 14) {
            section("Title") {
                TextField("Title", text: $state.title)
                    .textFieldStyle(.roundedBorder)
                    .font(Paper.Typography.callout)
            }
            section("Plan") {
                let shape = RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
                TextEditor(text: $state.prompt)
                    .font(Paper.Typography.callout)
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .frame(height: 112)
                    .background(Paper.elevated, in: shape)
                    .overlay { shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5).allowsHitTesting(false) }
                    .accessibilityLabel("Plan")
            }
            scheduleEditor
            if let problem = state.problem {
                Text(problem).foregroundStyle(Paper.errorInk).textSelection(.enabled)
            }
            HStack(spacing: 8) {
                Spacer(minLength: 0)
                Button("Cancel") {
                    state.endEditing()
                    state.problem = nil
                }
                .buttonStyle(CardButton(kind: .quiet))
                Button("Save") { Task { await state.save(using: selection) } }
                    .buttonStyle(CardButton(kind: .primary))
                    .disabled(!canSave)
                    .opacity(canSave ? 1 : 0.45)
            }
        }
        .disabled(state.pending)
    }

    private var canSave: Bool {
        !state.pending && [state.title, state.prompt, state.schedule.cron, state.timezone].allSatisfy {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }

    private var scheduleEditor: some View {
        frequencySection {
            Picker("Repeat", selection: Binding(
                get: { state.schedule.frequency },
                set: { state.schedule.select($0) }
            )) {
                ForEach(RoutineSchedule.Repeat.allCases, id: \.self) { repeatValue in
                    Text(repeatValue.rawValue).tag(repeatValue)
                }
            }
            if state.schedule.frequency == .custom {
                hairline
                TextField("Cron expression", text: $state.schedule.customCron,
                          prompt: Text("Minute hour day month weekday"))
                    .font(Paper.Typography.code)
                    .accessibilityLabel("Cron expression")
            } else {
                hairline
                DatePicker("Time", selection: $state.schedule.time, displayedComponents: .hourAndMinute)
                    .environment(\.calendar, RoutineSchedule.clockCalendar)
                    .environment(\.timeZone, .gmt)
                if state.schedule.frequency == .weekly {
                    hairline
                    Picker("Day", selection: $state.schedule.weekday) {
                        ForEach(weekdayNames.indices, id: \.self) { day in
                            Text(weekdayNames[day]).tag(day)
                        }
                    }
                } else if state.schedule.frequency == .monthly {
                    hairline
                    Picker("Day of month", selection: $state.schedule.monthDay) {
                        ForEach(1...31, id: \.self) { day in Text("\(day)").tag(day) }
                    }
                    if state.schedule.monthDay > 28 {
                        Text("Skips months without this day.")
                            .font(Paper.Typography.caption)
                            .foregroundStyle(Paper.inkSecondary)
                    }
                }
            }
            hairline
            LabeledContent("Time zone") {
                TextField("Time zone", text: $state.timezone)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 156)
            }
        }
    }

    private func scheduleSummary(_ schedule: RoutineSchedule, timezone: String) -> some View {
        frequencySection {
            LabeledContent("Repeat", value: schedule.frequency.rawValue)
            hairline
            if schedule.frequency == .custom {
                LabeledContent("Cron expression") {
                    Text(schedule.cron).monospaced().textSelection(.enabled)
                }
            } else {
                LabeledContent("Time", value: schedule.time.formatted(
                    Date.FormatStyle(date: .omitted, time: .shortened, timeZone: .gmt)))
                if schedule.frequency == .weekly {
                    hairline
                    LabeledContent("Day", value: weekdayNames[schedule.weekday])
                } else if schedule.frequency == .monthly {
                    hairline
                    LabeledContent("Day of month", value: "\(schedule.monthDay)")
                }
            }
            hairline
            LabeledContent("Time zone", value: timezone)
        }
    }

    private var weekdayNames: [String] { Calendar(identifier: .gregorian).weekdaySymbols }

    private func section<Content: View>(
        _ title: String, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(Paper.Typography.label)
            content()
        }
    }

    private func frequencySection<Rows: View>(@ViewBuilder rows: () -> Rows) -> some View {
        section("Frequency") {
            hairline
            VStack(alignment: .leading, spacing: 7, content: rows)
                .font(Paper.Typography.callout)
        }
    }

    private func label(_ raw: String) -> String {
        raw.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func date(_ raw: String, timezone: TimeZone = .current) -> String {
        guard let value = (try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(raw))
                ?? (try? Date.ISO8601FormatStyle().parse(raw)) else { return raw }
        return value.formatted(Date.FormatStyle(date: .abbreviated, time: .shortened, timeZone: timezone))
    }
}
