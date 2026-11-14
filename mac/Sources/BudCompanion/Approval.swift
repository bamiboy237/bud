import Foundation

/// Something bud is waiting on you for: an action to approve, or a question to answer. Built
/// from the session snapshot's pending list, which also carries every job's open approvals.
struct Approval: Identifiable, Equatable {
    let id: String
    let tool: String
    /// The job that asked, when it wasn't the main chat.
    let job: String?
    /// What will happen, in plain words: "Send email to dana@brightpath.example".
    let title: String
    /// The verb on the confirming button: "Send", "Schedule", "Approve".
    let verb: String
    /// What the receipt says once it's approved: "Sent", "Scheduled".
    var done: String {
        switch verb {
        case "Send": "Sent"
        case "Save draft": "Draft saved"
        case "Reply": "Replied"
        case "Schedule": "Scheduled"
        case "Move": "Moved"
        case "Cancel it": "Cancelled"
        case "Archive": "Archived"
        case "Delete": "Deleted"
        case "Add": "Added"
        default: "Approved"
        }
    }
    let symbol: String
    /// Short facts worth checking before you say yes: subject, time, place.
    let facts: [(label: String, value: String)]
    /// The text that will go out, when there is one, so you can read before approving.
    let body: String?
    /// For a question: the options bud offered. Empty means a free answer.
    let choices: [String]

    var isQuestion: Bool { tool == "ask_user" }

    static func == (a: Approval, b: Approval) -> Bool { a.id == b.id }

    init?(_ raw: [String: Any], jobs: [Int: String]) {
        guard let id = raw["approval_id"] as? String, let tool = raw["tool"] as? String else {
            return nil
        }
        let args = raw["args"] as? [String: Any] ?? [:]
        self.id = id
        self.tool = tool
        job = (raw["task_id"] as? Int).flatMap { jobs[$0] }
        choices = (args["choices"] as? [String]) ?? []
        let words = Self.describe(tool.lowercased(), args)
        title = tool == "ask_user" ? Self.text(args, "question") : words.title
        verb = words.verb
        symbol = words.symbol
        facts = Self.facts(args)
        body = Self.text(args, "body", "text", "description").nilIfEmpty
    }

    /// The same plain words the terminal uses for each kind of action.
    private static func describe(_ tool: String, _ args: [String: Any])
        -> (title: String, verb: String, symbol: String)
    {
        let to = recipients(args)
        let named = text(args, "title", "summary")
        if tool == "ask_user" { return ("", "Answer", "questionmark.bubble") }
        if tool.contains("draft") {
            return (to.isEmpty ? "Save an email draft" : "Save a draft to \(to)", "Save draft", "square.and.pencil")
        }
        if tool.contains("email") || tool.contains("reply") || tool.contains("send") {
            return (to.isEmpty ? "Send an email" : "Send email to \(to)", "Send", "paperplane")
        }
        if tool.contains("rsvp") {
            return ("Reply to the invite" + (named.isEmpty ? "" : " for \(named)"), "Reply", "calendar.badge.checkmark")
        }
        if tool.contains("event") || tool.contains("calendar") {
            if tool.contains("delete") || tool.contains("cancel") {
                return ("Cancel " + (named.isEmpty ? "the meeting" : "\"\(named)\""), "Cancel it", "calendar.badge.minus")
            }
            let move = tool.contains("move") || tool.contains("update") || tool.contains("patch")
            let what = named.isEmpty ? "a meeting" : "\"\(named)\""
            return (move ? "Move \(what)" : "Schedule \(what)", move ? "Move" : "Schedule", "calendar")
        }
        if tool.contains("archive") { return ("Archive email", "Archive", "archivebox") }
        if tool.contains("delete") || tool.contains("trash") || tool.contains("remove") {
            return ("Delete " + (named.isEmpty ? "this item" : named), "Delete", "trash")
        }
        if tool.contains("task") || tool.contains("todo") {
            return ("Add a to-do" + (named.isEmpty ? "" : ": \(named)"), "Add", "checklist")
        }
        let cleaned = tool.replacingOccurrences(of: "_", with: " ")
        return (cleaned.prefix(1).uppercased() + cleaned.dropFirst(), "Approve", "hand.raised")
    }

    private static func facts(_ args: [String: Any]) -> [(label: String, value: String)] {
        var out: [(String, String)] = []
        for (key, label) in [("subject", "Subject"), ("start", "When"), ("when", "When"),
                             ("location", "Where"), ("due", "Due")] {
            let value = text(args, key)
            if !value.isEmpty, !out.contains(where: { $0.0 == label }) {
                out.append((label, key == "start" ? readableTime(value) : value))
            }
        }
        return out
    }

    private static func recipients(_ args: [String: Any]) -> String {
        for key in ["to", "recipient", "recipients"] {
            if let list = args[key] as? [String], !list.isEmpty { return list.joined(separator: ", ") }
            if let one = args[key] as? String, !one.isEmpty { return one }
        }
        return ""
    }

    private static func text(_ args: [String: Any], _ keys: String...) -> String {
        for key in keys {
            if let value = args[key] as? String, !value.isEmpty { return value }
        }
        return ""
    }

    /// "2026-11-06T11:00:00-06:00" reads as "Fri, Nov 6 at 11:00 AM".
    private static func readableTime(_ value: String) -> String {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime]
        guard let date = parser.date(from: value) ?? {
            parser.formatOptions = [.withFullDate, .withTime, .withColonSeparatorInTime]
            return parser.date(from: value)
        }() else { return value }
        return date.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day().hour().minute())
    }
}

extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
