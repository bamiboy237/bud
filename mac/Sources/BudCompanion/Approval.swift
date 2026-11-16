import Foundation

/// Something bud is waiting on you for: an action to approve, or a question to answer. Built
/// from the session snapshot's pending list, which also carries every job's open approvals.
struct Approval: Identifiable, Equatable {
    let id: String
    let tool: String
    let requestID: String?
    let emailPreview: EmailPreview?
    let expectsOriginalEmail: Bool
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
        case "Mark read": "Marked read"
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
    let questions: [Question]
    let isQuestionList: Bool

    var isQuestion: Bool { tool == "ask_user" }

    var menuTitle: String {
        let subject = facts.first { $0.label == "Subject" }?.value ?? emailPreview?.subject
        return subject.map { "\(verb): \($0)" } ?? title
    }

    /// The one line under a collapsed row: what the action is about, in the fewest words.
    var rowDetail: String? {
        if let subject = emailPreview?.subject { return subject }
        if let job { return "For the job “\(job)”" }
        if let first = facts.first { return "\(first.label): \(first.value)" }
        return body?.split(separator: "\n").first.map(String.init)
    }

    func batch(in approvals: [Approval]) -> [Approval] {
        guard !isQuestion, let requestID else { return [] }
        return approvals.filter { !$0.isQuestion && $0.requestID == requestID }
    }

    func retryBatch(in approvals: [Approval], failures: [String: String]) -> [Approval] {
        batch(in: approvals).filter { failures[$0.id] != nil }
    }

    static func == (a: Approval, b: Approval) -> Bool { a.id == b.id }

    init?(_ raw: [String: Any], jobs: [Int: String]) {
        guard let id = raw["approval_id"] as? String, let tool = raw["tool"] as? String else {
            return nil
        }
        let args = raw["args"] as? [String: Any] ?? [:]
        self.id = id
        self.tool = tool
        requestID = (raw["request_id"] as? String)?.nilIfEmpty.map { "request:\($0)" }
            ?? (raw["run_id"] as? String)?.nilIfEmpty.map {
                "task:\(raw["task_id"] as? Int ?? 0):run:\($0)"
            }
        emailPreview = (raw["preview"] as? [String: Any]).flatMap(EmailPreview.init)
        expectsOriginalEmail = ["archive_email", "mark_read", "draft_reply"].contains(tool)
            || args["thread_id"] != nil || args["draft_id"] != nil
        job = (raw["task_id"] as? Int).flatMap { jobs[$0] }
        let parsed = Question.parse(args)
        questions = parsed.questions
        isQuestionList = parsed.isList
        let words = Self.describe(tool.lowercased(), args)
        title = tool == "ask_user" ? (questions.first?.text ?? "") : words.title
        verb = words.verb
        symbol = words.symbol
        facts = Self.facts(args)
        if args["is_html"] as? Bool == true {
            // Approvals saved before previews existed have no proposed text; show the body
            // they carry rather than hiding what will be sent.
            body = ((raw["preview"] as? [String: Any])?["proposed_body_text"] as? String)
                .flatMap(\.nilIfEmpty)
                ?? Self.text(args, "body", "body_text", "text", "description").nilIfEmpty
        } else {
            body = Self.text(args, "body", "body_text", "text", "description").nilIfEmpty
        }
    }

    /// The same plain words the terminal uses for each kind of action.
    private static func describe(_ tool: String, _ args: [String: Any])
        -> (title: String, verb: String, symbol: String)
    {
        let to = recipients(args)
        let named = text(args, "title", "summary")
        if tool == "ask_user" { return ("", "Answer", "questionmark.bubble") }
        if tool == "archive_email" { return ("Archive email", "Archive", "archivebox") }
        if tool == "mark_read" { return ("Mark email read", "Mark read", "envelope.open") }
        if ["draft_reply", "gmail_create_email_draft"].contains(tool) {
            return (to.isEmpty ? "Save an email draft" : "Save a draft to \(to)", "Save draft", "square.and.pencil")
        }
        if ["send_email", "gmail_send_email"].contains(tool) {
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
        let to = recipients(args)
        if !to.isEmpty { out.append(("To", to)) }
        for (key, label) in [("extra_recipients", "Also to"), ("cc", "Cc"), ("bcc", "Bcc")] {
            let value = (args[key] as? [String])?.joined(separator: ", ") ?? text(args, key)
            if !value.isEmpty { out.append((label, value)) }
        }
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
        for key in ["to", "recipient", "recipients", "recipient_email"] {
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

struct Question: Equatable, Sendable {
    let header: String
    let text: String
    let options: [QuestionOption]
    let multiSelect: Bool

    static func parse(_ args: [String: Any]) -> (questions: [Question], isList: Bool) {
        if let raw = args["questions"] as? [[String: Any]] {
            let parsed = raw.compactMap(Question.init)
            if !parsed.isEmpty { return (parsed, true) }
        }
        let text = args["question"] as? String ?? ""
        let choices = (args["choices"] as? [String]) ?? []
        return ([Question(header: "Question", text: text.isEmpty ? "bud has a question" : text,
                          options: choices.filter { !$0.isEmpty }.map { QuestionOption(label: $0) },
                          multiSelect: false)], false)
    }

    private init?(_ raw: [String: Any]) {
        guard let header = (raw["header"] as? String)?.nilIfEmpty,
              let text = (raw["question"] as? String)?.nilIfEmpty else { return nil }
        self.header = header
        self.text = text
        options = (raw["options"] as? [[String: Any]] ?? []).compactMap(QuestionOption.init)
        multiSelect = raw["multi_select"] as? Bool ?? false
    }

    private init(header: String, text: String, options: [QuestionOption], multiSelect: Bool) {
        self.header = header
        self.text = text
        self.options = options
        self.multiSelect = multiSelect
    }
}

struct QuestionOption: Equatable, Sendable {
    let label: String
    let description: String?

    fileprivate init?(_ raw: [String: Any]) {
        guard let label = (raw["label"] as? String)?.nilIfEmpty else { return nil }
        self.label = label
        description = (raw["description"] as? String)?.nilIfEmpty
    }

    fileprivate init(label: String) {
        self.label = label
        description = nil
    }
}

struct EmailPreview {
    let subject: String?
    let messages: [EmailMessage]
    let unavailableReason: String?

    init?(_ raw: [String: Any]) {
        guard raw["kind"] as? String == "email" else { return nil }
        subject = (raw["subject"] as? String)?.nilIfEmpty
        messages = (raw["messages"] as? [[String: Any]] ?? []).map(EmailMessage.init)
        unavailableReason = (raw["unavailable_reason"] as? String)?.nilIfEmpty
    }
}

struct EmailMessage {
    let sender: String?
    let to: [String]
    let cc: [String]
    let bcc: [String]
    let sentAt: String?
    let body: String?

    init(_ raw: [String: Any]) {
        sender = (raw["sender"] as? String)?.nilIfEmpty
        to = raw["to"] as? [String] ?? []
        cc = raw["cc"] as? [String] ?? []
        bcc = raw["bcc"] as? [String] ?? []
        sentAt = (raw["sent_at"] as? String)?.nilIfEmpty
        if raw["body_format"] as? String == "html" {
            body = (raw["body_text"] as? String)?.nilIfEmpty
        } else {
            body = (raw["body"] as? String)?.nilIfEmpty
        }
    }
}

struct EmailParagraph: Identifiable {
    let id: Int
    let text: String
    let quoted: Bool

    static func parse(_ text: String) -> [EmailParagraph] {
        var paragraphs: [EmailParagraph] = []
        var lines: [String] = []
        var quoted = false
        func flush() {
            if !lines.isEmpty {
                paragraphs.append(Self(id: paragraphs.count, text: lines.joined(separator: "\n"), quoted: quoted))
                lines = []
            }
        }
        for line in text.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let isQuote = trimmed.hasPrefix(">")
            if trimmed.isEmpty {
                flush()
            } else {
                if isQuote != quoted { flush() }
                quoted = isQuote
                lines.append(isQuote ? String(trimmed.dropFirst()).trimmingCharacters(in: .whitespaces) : line)
            }
        }
        flush()
        return paragraphs
    }
}

extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
