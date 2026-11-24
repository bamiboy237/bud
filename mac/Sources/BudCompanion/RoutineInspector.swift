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
        Button(action: open) {
            HStack(spacing: 10) {
                Image(systemName: "clock.arrow.circlepath").foregroundStyle(Paper.accent)
                VStack(alignment: .leading, spacing: 3) {
                    Text(card.title).font(Paper.Typography.callout).lineLimit(2)
                    Text(card.label).font(Paper.Typography.ledgerMeta)
                        .foregroundStyle(Paper.inkSecondary)
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.right").font(.caption2)
                    .foregroundStyle(Paper.inkSecondary)
            }
            .padding(10)
            .frame(maxWidth: 320, alignment: .leading)
        }
        .buttonStyle(PaperHit(shape: RoundedRectangle(cornerRadius: Paper.radiusMD), fill: Paper.elevated))
        .accessibilityLabel("\(card.title), \(card.label)")
    }
}

private struct RoutineSummary: Decodable, Identifiable {
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

private struct RoutineRun: Decodable, Identifiable {
    let occurrenceId: String
    let scheduledFor: String
    let executionStatus: String
    let deliveryStatus: String
    let reportPath: String?
    var id: String { occurrenceId }
}

@MainActor
private final class RoutineInspectorState: ObservableObject {
    @Published var list: [RoutineSummary] = []
    @Published var routine: RoutineSummary?
    @Published var report: String?
    @Published var listing = true
    @Published var pending = true
    @Published var problem: String?
    @Published var editing = false
    @Published var title = ""
    @Published var prompt = ""
    @Published var schedule = RoutineSchedule(cron: "0 9 * * *")
    @Published var timezone = ""
    @Published var markedRun: String?
    @Published var triggerID = UUID().uuidString
}

@MainActor
struct RoutineInspector: View {
    let selection: RoutineSelection
    let close: () -> Void
    @StateObject private var state = RoutineInspectorState()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                if state.report != nil || !state.listing {
                    Button { back() } label: { Image(systemName: "chevron.left") }
                        .buttonStyle(IconButton(size: 24))
                        .accessibilityLabel("Back")
                        .disabled(state.pending)
                }
                Text(heading)
                    .font(Paper.Typography.callout).lineLimit(1)
                Spacer()
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(IconButton(size: 24))
                    .accessibilityLabel("Close routines")
            }
            .padding(12)
            Divider()
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
                        ForEach(state.list) { item in
                            Button {
                                Task { await show(item.id) }
                            } label: {
                                HStack {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(item.title).font(Paper.Typography.callout)
                                        Text(item.status.capitalized)
                                            .foregroundStyle(Paper.inkSecondary).font(.caption)
                                    }
                                    Spacer()
                                    Image(systemName: "chevron.right").font(.caption2)
                                }
                                .padding(9)
                            }
                            .buttonStyle(PaperHit(
                                shape: RoundedRectangle(cornerRadius: Paper.radiusMD), fill: Paper.elevated))
                            .disabled(state.pending)
                        }
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
            if let id = selection.routineID {
                state.markedRun = selection.occurrenceID
                await show(id)
            } else {
                await loadList()
            }
        }
    }

    private var heading: String {
        if state.report != nil { return "Run report" }
        if state.listing { return "Routines" }
        return state.editing ? "Edit routine" : "Routine"
    }

    @ViewBuilder
    private func detail(_ value: RoutineSummary) -> some View {
        if state.editing {
            editor(value)
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
                .background(Paper.elevated, in: RoundedRectangle(cornerRadius: Paper.radiusSM))
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
                    Button("Pause") { Task { await change(value.id, path: "/pause") } }
                        .buttonStyle(CardButton(kind: .quiet))
                } else if value.status == "paused", value.pauseReason != "uncertain" {
                    Button("Resume") { Task { await change(value.id, path: "/resume") } }
                        .buttonStyle(CardButton(kind: .primary))
                }
                Spacer(minLength: 0)
                Button("Edit") {
                    state.title = value.title
                    state.prompt = value.prompt
                    state.schedule = RoutineSchedule(cron: value.cron)
                    state.timezone = value.timezone
                    state.editing = true
                }
                .buttonStyle(CardButton(kind: .secondary))
                if value.status == "active" {
                    Button("Run now") {
                        Task { await change(value.id, path: "/run-now", body: ["trigger_id": state.triggerID]) }
                    }
                    .buttonStyle(CardButton(kind: .primary))
                }
            }
            .disabled(state.pending)
            .opacity(state.pending ? 0.55 : 1)
        }
        if !state.editing && !value.recentOccurrences.isEmpty {
            Divider()
            Text("Run history").font(Paper.Typography.callout)
            ForEach(value.recentOccurrences) { run in
                VStack(alignment: .leading, spacing: 4) {
                    Text(date(run.scheduledFor))
                    Text("\(label(run.executionStatus)) · \(label(run.deliveryStatus))")
                        .font(.caption).foregroundStyle(Paper.inkSecondary)
                    if run.reportPath != nil {
                        Button("Open report") { Task { await openReport(value.id, run.id) } }
                            .buttonStyle(.link).disabled(state.pending)
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    run.id == state.markedRun ? Paper.accentFill : Paper.elevated,
                    in: RoundedRectangle(cornerRadius: Paper.radiusMD))
            }
        }
    }

    private func editor(_ value: RoutineSummary) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            section("Title") {
                TextField("Title", text: $state.title)
                    .textFieldStyle(.roundedBorder)
                    .font(Paper.Typography.callout)
            }
            section("Plan") {
                TextEditor(text: $state.prompt)
                    .font(Paper.Typography.callout)
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .frame(height: 112)
                    .background(Paper.elevated, in: RoundedRectangle(cornerRadius: Paper.radiusSM))
                    .accessibilityLabel("Plan")
            }
            scheduleEditor
            if let problem = state.problem {
                Text(problem).foregroundStyle(Paper.errorInk).textSelection(.enabled)
            }
            HStack(spacing: 8) {
                Spacer(minLength: 0)
                Button("Cancel") {
                    state.editing = false
                    state.problem = nil
                }
                .buttonStyle(CardButton(kind: .quiet))
                Button("Save") { Task { await save(value) } }
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
                Divider()
                TextField("Cron expression", text: $state.schedule.customCron,
                          prompt: Text("Minute hour day month weekday"))
                    .font(Paper.Typography.code)
                    .accessibilityLabel("Cron expression")
            } else {
                Divider()
                DatePicker("Time", selection: $state.schedule.time, displayedComponents: .hourAndMinute)
                    .environment(\.calendar, RoutineSchedule.clockCalendar)
                    .environment(\.timeZone, .gmt)
                if state.schedule.frequency == .weekly {
                    Divider()
                    Picker("Day", selection: $state.schedule.weekday) {
                        ForEach(weekdayNames.indices, id: \.self) { day in
                            Text(weekdayNames[day]).tag(day)
                        }
                    }
                } else if state.schedule.frequency == .monthly {
                    Divider()
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
            Divider()
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
            Divider()
            if schedule.frequency == .custom {
                LabeledContent("Cron expression") {
                    Text(schedule.cron).monospaced().textSelection(.enabled)
                }
            } else {
                LabeledContent("Time", value: schedule.time.formatted(
                    Date.FormatStyle(date: .omitted, time: .shortened, timeZone: .gmt)))
                if schedule.frequency == .weekly {
                    Divider()
                    LabeledContent("Day", value: weekdayNames[schedule.weekday])
                } else if schedule.frequency == .monthly {
                    Divider()
                    LabeledContent("Day of month", value: "\(schedule.monthDay)")
                }
            }
            Divider()
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
            VStack(alignment: .leading, spacing: 7, content: rows)
                .font(Paper.Typography.callout)
                .padding(10)
                .background(Paper.elevated, in: RoundedRectangle(cornerRadius: Paper.radiusMD))
        }
    }

    private func save(_ value: RoutineSummary) async {
        var changes = ["title": state.title, "prompt": state.prompt, "timezone": state.timezone]
        let cron = state.schedule.cron
        if cron != RoutineSchedule(cron: value.cron).cron { changes["cron"] = cron }
        await change(value.id, method: "PATCH", body: changes)
    }

    private func label(_ raw: String) -> String {
        raw.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func date(_ raw: String, timezone: TimeZone = .current) -> String {
        guard let value = (try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(raw))
                ?? (try? Date.ISO8601FormatStyle().parse(raw)) else { return raw }
        return value.formatted(Date.FormatStyle(date: .abbreviated, time: .shortened, timeZone: timezone))
    }

    private func decode(_ data: [String: Any]) throws -> RoutineSummary {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(RoutineSummary.self, from: JSONSerialization.data(withJSONObject: data))
    }

    private func back() {
        if state.report != nil {
            state.report = nil
        } else {
            state.markedRun = nil
            state.listing = true
            state.editing = false
            Task { await loadList() }
        }
    }

    private func loadList() async {
        state.pending = true
        state.problem = nil
        defer { state.pending = false }
        do {
            let response = try await selection.connection.send(
                "GET", "routines", queryItems: [URLQueryItem(name: "feed_id", value: selection.session)])
            state.list = try (response["routines"] as? [[String: Any]] ?? []).map(decode)
        } catch { state.problem = "\(error)" }
    }

    private func show(_ id: String) async {
        state.listing = false
        state.pending = true
        state.problem = nil
        defer { state.pending = false }
        do {
            let response = try await selection.connection.send("GET", "routines/\(id)")
            state.routine = try decode(response)
        } catch { state.problem = "\(error)" }
    }

    private func change(
        _ id: String, path: String = "", method: String = "POST", body: [String: String]? = nil
    ) async {
        guard !state.pending else { return }
        state.pending = true
        state.problem = nil
        defer { state.pending = false }
        do {
            _ = try await selection.connection.send(method, "routines/\(id)\(path)", json: body)
            if path == "/run-now" { state.triggerID = UUID().uuidString }
            state.editing = false
            await show(id)
        } catch { state.problem = "\(error)" }
    }

    private func openReport(_ routineID: String, _ occurrenceID: String) async {
        state.pending = true
        state.problem = nil
        defer { state.pending = false }
        do {
            let response = try await selection.connection.send(
                "GET", "routines/\(routineID)/occurrences/\(occurrenceID)/report")
            state.report = response["content"] as? String
        } catch { state.problem = "\(error)" }
    }
}
