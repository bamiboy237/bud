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
struct RoutineInspector: View {
    let selection: RoutineSelection
    let close: () -> Void
    @State private var list: [RoutineSummary] = []
    @State private var routine: RoutineSummary?
    @State private var report: String?
    @State private var listing = true
    @State private var pending = true
    @State private var problem: String?
    @State private var editing = false
    @State private var title = ""
    @State private var prompt = ""
    @State private var cron = ""
    @State private var timezone = ""
    @State private var markedRun: String?
    @State private var triggerID = UUID().uuidString

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                if report != nil || !listing {
                    Button { back() } label: { Image(systemName: "chevron.left") }
                        .buttonStyle(IconButton(size: 24))
                        .accessibilityLabel("Back")
                        .disabled(pending)
                }
                Text(report != nil ? "Run report" : listing ? "Routines" : routine?.title ?? "Routine")
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
                    if let problem {
                        Text(problem).foregroundStyle(Paper.errorInk).textSelection(.enabled)
                    }
                    if let report {
                        MarkdownView(report, rich: true).textSelection(.enabled)
                    } else if listing {
                        if list.isEmpty, !pending, problem == nil {
                            Text("Ask Bud in chat to schedule a routine.")
                                .foregroundStyle(Paper.inkSecondary)
                        }
                        ForEach(list) { item in
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
                    } else if let routine {
                        detail(routine)
                    }
                    if pending { ProgressView().controlSize(.small) }
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
                markedRun = selection.occurrenceID
                await show(id)
            } else {
                await loadList()
            }
        }
    }

    @ViewBuilder
    private func detail(_ value: RoutineSummary) -> some View {
        if editing {
            VStack(alignment: .leading, spacing: 10) {
                LabeledContent("Name") { TextField("Routine name", text: $title) }
                LabeledContent("Schedule") { TextField("Five-field cron", text: $cron) }
                LabeledContent("Time zone") { TextField("America/Chicago", text: $timezone) }
                Text("Instructions").font(.caption)
                TextEditor(text: $prompt).frame(minHeight: 96)
                HStack {
                    Button("Cancel") { editing = false }
                    Spacer()
                    Button("Save") {
                        Task {
                            await change(value.id, method: "PATCH", body: [
                                "title": title, "prompt": prompt, "cron": cron, "timezone": timezone,
                            ])
                        }
                    }
                    .disabled(pending || [title, prompt, cron, timezone].contains {
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
                    (title, prompt, cron, timezone) = (value.title, value.prompt, value.cron, value.timezone)
                    editing = true
                }
                if value.status == "active" {
                    Button("Pause") { Task { await change(value.id, path: "/pause") } }
                    Button("Run now") {
                        Task { await change(value.id, path: "/run-now", body: ["trigger_id": triggerID]) }
                    }
                } else if value.status == "paused", value.pauseReason != "uncertain" {
                    Button("Resume") { Task { await change(value.id, path: "/resume") } }
                }
            }
            .disabled(pending)
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
                            .buttonStyle(.link).disabled(pending)
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    run.id == markedRun ? Paper.accentFill : Paper.elevated,
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
        if report != nil {
            report = nil
        } else {
            markedRun = nil
            listing = true
            editing = false
            Task { await loadList() }
        }
    }

    private func loadList() async {
        pending = true
        problem = nil
        defer { pending = false }
        do {
            let response = try await selection.connection.send(
                "GET", "routines", queryItems: [URLQueryItem(name: "feed_id", value: selection.session)])
            list = try (response["routines"] as? [[String: Any]] ?? []).map(decode)
        } catch { problem = "\(error)" }
    }

    private func show(_ id: String) async {
        listing = false
        pending = true
        problem = nil
        defer { pending = false }
        do {
            let response = try await selection.connection.send("GET", "routines/\(id)")
            routine = try decode(response)
        } catch { problem = "\(error)" }
    }

    private func change(
        _ id: String, path: String = "", method: String = "POST", body: [String: Any]? = nil
    ) async {
        guard !pending else { return }
        pending = true
        problem = nil
        defer { pending = false }
        do {
            _ = try await selection.connection.send(method, "routines/\(id)\(path)", json: body)
            if path == "/run-now" { triggerID = UUID().uuidString }
            editing = false
            await show(id)
        } catch { problem = "\(error)" }
    }

    private func openReport(_ routineID: String, _ occurrenceID: String) async {
        pending = true
        problem = nil
        defer { pending = false }
        do {
            let response = try await selection.connection.send(
                "GET", "routines/\(routineID)/occurrences/\(occurrenceID)/report")
            report = response["content"] as? String
        } catch { problem = "\(error)" }
    }
}
