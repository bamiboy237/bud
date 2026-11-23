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
            .background(Paper.elevated, in: RoundedRectangle(cornerRadius: Paper.radiusMD))
        }
        .buttonStyle(.plain)
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
    @Published var cron = ""
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
                Text(state.report != nil ? "Run report" : state.listing ? "Routines" : state.routine?.title ?? "Routine")
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
                    if let problem = state.problem {
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
                                .background(Paper.elevated, in: RoundedRectangle(cornerRadius: Paper.radiusMD))
                            }.buttonStyle(.plain)
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

    @ViewBuilder
    private func detail(_ value: RoutineSummary) -> some View {
        if state.editing {
            VStack(alignment: .leading, spacing: 10) {
                LabeledContent("Name") { TextField("Routine name", text: $state.title) }
                LabeledContent("Schedule") { TextField("Five-field cron", text: $state.cron) }
                LabeledContent("Time zone") { TextField("America/Chicago", text: $state.timezone) }
                Text("Instructions").font(.caption)
                TextEditor(text: $state.prompt).frame(minHeight: 96)
                HStack {
                    Button("Cancel") { state.editing = false }
                    Spacer()
                    Button("Save") {
                        Task {
                            await change(value.id, method: "PATCH", body: [
                                "title": state.title, "prompt": state.prompt, "cron": state.cron, "timezone": state.timezone,
                            ])
                        }
                    }
                    .disabled(state.pending || [state.title, state.prompt, state.cron, state.timezone].contains {
                        $0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    })
                }
            }
        } else {
            Text(value.status.capitalized).font(.caption).foregroundStyle(Paper.inkSecondary)
            Text(value.prompt).textSelection(.enabled)
            LabeledContent("Schedule", value: "\(value.cron) · \(value.timezone)")
            if let next = value.nextRunAt { LabeledContent("Next run", value: date(next)) }
            if value.pauseReason == "uncertain" {
                Text("Review the last run before resuming. Bud could not confirm an action.")
                    .foregroundStyle(Paper.inkSecondary)
            }
            HStack {
                Button("Edit") {
                    (state.title, state.prompt, state.cron, state.timezone) = (value.title, value.prompt, value.cron, value.timezone)
                    state.editing = true
                }
                if value.status == "active" {
                    Button("Pause") { Task { await change(value.id, path: "/pause") } }
                    Button("Run now") {
                        Task { await change(value.id, path: "/run-now", body: ["trigger_id": state.triggerID]) }
                    }
                } else if value.status == "paused", value.pauseReason != "uncertain" {
                    Button("Resume") { Task { await change(value.id, path: "/resume") } }
                }
            }
            .disabled(state.pending)
        }
        if !value.recentOccurrences.isEmpty {
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

    private func label(_ raw: String) -> String {
        raw.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func date(_ raw: String) -> String {
        guard let value = (try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(raw))
                ?? (try? Date.ISO8601FormatStyle().parse(raw)) else { return raw }
        return value.formatted(date: .abbreviated, time: .shortened)
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
