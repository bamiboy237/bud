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
    let action: String
    /// Bud asking, in its own voice: "Can I send email to dana@brightpath.example?" A question
    /// card shows the question itself.
    var title: String {
        guard !isQuestion else { return action }
        guard let first = action.first else { return "Can I go ahead?" }
        // Lowercase a sentence-case first word ("Archive" to "archive"), not a name or path.
        let second = action.dropFirst().first
        let lead = second.map(\.isLowercase) ?? true ? first.lowercased() : String(first)
        return "Can I \(lead)\(action.dropFirst())?"
    }
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
        case "Save": "Saved"
        case "Replace": "Replaced"
        case "Turn on": "Turned on"
        case "Open": "Opened"
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

    /// True when bud may keep this tool's permission: the card then offers "Always allow".
    /// Sends, real Gmail and Calendar writes, and skill installs never are.
    let grantable: Bool

    /// "Archive email without asking again": the action's own words, so the grant is clear.
    var alwaysAllowHelp: String { "\(action) without asking again" }

    /// "Archive email will always run": what a standing grant leaves behind.
    var alwaysAllowDone: String { "\(action) will always run" }

    var menuTitle: String {
        let subject = facts.first { $0.label == "Subject" }?.value ?? emailPreview?.subject
        return subject.map { "\(verb): \($0)" } ?? title
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
        grantable = raw["grantable"] as? Bool ?? false
        expectsOriginalEmail = ["archive_email", "mark_read", "draft_reply"].contains(tool)
            || args["thread_id"] != nil || args["draft_id"] != nil
        job = (raw["task_id"] as? Int).flatMap { jobs[$0] }
        let parsed = Question.parse(args)
        questions = parsed.questions
        isQuestionList = parsed.isList
        let words = Self.describe(tool.lowercased(), args)
        action = tool == "ask_user" ? (questions.first?.text ?? "") : words.title
        verb = words.verb
        symbol = words.symbol
        let review = raw["review"] as? [String: Any] ?? [:]
        // enable_browser's looked-up fact is the caution, which the body already shows.
        facts = Self.facts(args) + Self.reviewFacts(tool == "enable_browser" ? [:] : review)
        if tool == "enable_browser" {
            // Fixed words from the app, never from the model, so the caution cannot be edited.
            // The card shows this line; the full caution sits under "What this allows".
            body = Self.browserSummary
        } else if args["is_html"] as? Bool == true {
            // Approvals saved before previews existed have no proposed text; show the body
            // they carry rather than hiding what will be sent.
            body = ((raw["preview"] as? [String: Any])?["proposed_body_text"] as? String)
                .flatMap(\.nilIfEmpty)
                ?? Self.text(args, "body", "body_text", "text", "description").nilIfEmpty
        } else {
            body = Self.text(args, "body", "body_text", "text", "description", "content").nilIfEmpty
        }
    }

    /// Approvals that are a plain yes or no, with nothing to revise.
    static let yesOrNo: Set<String> = [
        "enable_browser", "open_chrome", "read_local_file", "list_local_folder",
    ]

    /// One line for the card that turns the browser on.
    static let browserSummary = "Signed in as you, including Gmail and Calendar. Risky steps, "
        + "such as paying, sending, or deleting, still ask you first."

    /// What turning the browser on allows. Kept in step with bud.browser.CAUTION.
    static let browserCaution = "Bud will use your own Chrome, signed in as you, including "
        + "Gmail and Calendar: it can open pages, click, type, and submit forms. Risky steps, "
        + "such as paying, sending, deleting, or typing a password, ask you first. A page can "
        + "still try to trick Bud. Other apps lose the browse extension while Bud uses it. "
        + "Turn it off at any time."

    /// The same plain words the terminal uses for each kind of action.
    private static func describe(_ tool: String, _ args: [String: Any])
        -> (title: String, verb: String, symbol: String)
    {
        let to = recipients(args)
        let named = text(args, "title", "summary")
        if tool == "ask_user" { return ("", "Answer", "questionmark.bubble") }
        if tool == "enable_browser" { return ("Use your Chrome", "Turn on", "globe") }
        if tool.hasPrefix("browser_") { return browserStep(String(tool.dropFirst(8)), args) }
        if tool == "open_chrome" { return ("Open Chrome", "Open", "globe") }
        if tool == "music_control" {
            let action = text(args, "action")
            let words = ["play": "Play music", "pause": "Pause the music",
                         "next": "Skip to the next song", "previous": "Go back a song"]
            return (words[action] ?? "Control Music", action == "pause" ? "Pause" : "Go", "music.note")
        }
        if tool == "notes_create" {
            let title = text(args, "title")
            return (title.isEmpty ? "Create a note" : "Create the note \"\(title)\"", "Create", "note.text.badge.plus")
        }
        if tool == "reminders_add" {
            let title = text(args, "title")
            return (title.isEmpty ? "Add a reminder" : "Add the reminder \"\(title)\"", "Add", "checklist")
        }
        if tool == "read_local_file" {
            let name = (text(args, "path") as NSString).lastPathComponent
            return (name.isEmpty ? "Read one of your files" : "Read \(name)", "Read", "doc.text.magnifyingglass")
        }
        if tool == "list_local_folder" {
            let name = (text(args, "path") as NSString).lastPathComponent
            return (name.isEmpty ? "Look in one of your folders" : "Look in \(name)", "Allow", "folder")
        }
        if tool == "workspace_create" {
            let path = text(args, "path")
            return (path.isEmpty ? "Save a file" : "Save \(path)", "Save", "doc.badge.plus")
        }
        if tool == "workspace_update" {
            let path = text(args, "path")
            return (path.isEmpty ? "Replace a file" : "Replace \(path)", "Replace", "doc.badge.arrow.up")
        }
        if tool == "archive_email" { return ("Archive this email", "Archive", "archivebox") }
        if tool == "mark_read" { return ("Mark this email read", "Mark read", "envelope.open") }
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

    /// One step in the owner's browser, in plain words.
    private static func browserStep(_ step: String, _ args: [String: Any])
        -> (title: String, verb: String, symbol: String)
    {
        switch step {
        case "navigate":
            let url = text(args, "url")
            // The site, not the whole address: the Address row below shows that.
            let site = URL(string: url)?.host() ?? url
            return (site.isEmpty ? "Open a page" : "Open \(site)", "Open", "globe")
        case "click": return ("Click on a page", "Click", "cursorarrow.click")
        case "fill", "type": return ("Type into a page", "Type", "keyboard")
        case "press": return ("Press a key on a page", "Press", "keyboard")
        case "select": return ("Choose an option on a page", "Choose", "list.bullet")
        case "hover": return ("Point at something on a page", "Approve", "cursorarrow")
        case "drag": return ("Drag on a page", "Drag", "hand.draw")
        case "dialog": return ("Answer a page's dialog", "Answer", "macwindow")
        case "tabs": return ("Open or close browser tabs", "Approve", "square.on.square")
        case "upload": return ("Upload a file to a page", "Upload", "arrow.up.doc")
        default: return ("Use your browser", "Approve", "globe")
        }
    }

    /// Why bud waits: facts it looked up itself, such as the site a tab shows, and the
    /// reviewer's reason. Both are fixed or looked-up text, never the model's words.
    private static func reviewFacts(_ review: [String: Any]) -> [(label: String, value: String)] {
        var out: [(String, String)] = []
        if let details = (review["details"] as? String)?.nilIfEmpty { out.append(("Checked", details)) }
        if let reason = (review["reason"] as? String)?.nilIfEmpty { out.append(("Why", reason)) }
        return out
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
                             ("location", "Where"), ("due", "Due"), ("path", "File"),
                             ("url", "Address"), ("key", "Key"), ("ref", "Element"),
                             ("selector", "Element"), ("action", "Action")] {
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

/// The body a decision sends. `remember` asks bud to keep this tool's permission, so later
/// calls of the same tool run without asking.
struct ApprovalDecision {
    var approved: Bool
    var remember = false

    /// A standing grant: yes now, and yes to this tool from now on.
    static let always = ApprovalDecision(approved: true, remember: true)

    var body: [String: any Sendable] {
        remember ? ["approved": approved, "remember": true] : ["approved": approved]
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
