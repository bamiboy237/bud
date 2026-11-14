import Foundation

/// What bud is doing, in plain words, from the tool calls in its event stream.
///
/// bud rarely calls a tool directly: it writes a small Python program in one `run_code` call
/// (`await asyncio.gather(list_events(...), search_email(query='Dana'))`). So the words come
/// from the functions named inside that code, with their arguments read loosely from the
/// source. Names are matched by keyword, not exactly, because real accounts add Composio tools
/// (`GMAIL_FETCH_EMAILS`) that the simulated world doesn't have.
enum Activity {
    /// Plain-words steps for one tool call, in the order the code calls them. Plumbing (the
    /// clock, printing, gathering) gives none; a call with nothing recognisable gives one
    /// general step, so the line still moves.
    static func steps(tool: String, args: [String: Any]) -> [String] {
        if tool == "run_code" {
            let code = args["code"] as? String ?? ""
            let found = calls(in: code).compactMap { describe($0.name, $0.args) }
            return found.isEmpty ? ["Working through the details"] : dedupe(found)
        }
        let flat = args.compactMapValues { value -> String? in
            if let text = value as? String { return text }
            if let list = value as? [String] { return list.joined(separator: ", ") }
            return nil
        }
        return describe(tool, flat).map { [$0] } ?? []
    }

    /// One function call as words, or nil when it's plumbing.
    private static func describe(_ rawName: String, _ args: [String: String]) -> String? {
        let name = rawName.lowercased()
        let app = composioApp(rawName)
        func raw(_ keys: [String]) -> String? {
            keys.lazy.compactMap { args[$0] }.first { !$0.isEmpty }
        }
        /// A value to quote in the words, cut short so the line stays one line.
        func arg(_ keys: String...) -> String? { raw(keys).map(short) }
        if ignored.contains(name) { return nil }

        // Email
        if name.contains("draft") {
            if let to = arg("to", "recipient") { return "Drafting an email to \(to)" }
            return name.contains("list") || name.contains("read") ? "Looking at your drafts" : "Drafting a reply"
        }
        if name.contains("send") && (name.contains("email") || name.contains("mail") || app == "Gmail") {
            return arg("to", "recipient").map { "Getting the email to \($0) ready" } ?? "Getting the email ready"
        }
        if name.contains("archive") || name.contains("mark_read") { return "Tidying your inbox" }
        if name.contains("thread") || name.contains("message") && app == "Gmail" { return "Reading an email" }
        if name.contains("email") || name.contains("mail") || app == "Gmail" {
            if let who = arg("sender", "from") { return "Looking for email from \(who)" }
            if let query = arg("query", "q") { return "Searching your email for “\(query)”" }
            return "Checking your email"
        }

        // Calendar
        if name.contains("rsvp") { return "Answering an invite" }
        if name.contains("event") || name.contains("calendar") || app == "Calendar" {
            let title = arg("title", "summary").map { "“\($0)”" }
            if name.contains("delete") || name.contains("cancel") { return "Cancelling \(title ?? "a meeting")" }
            if name.contains("move") || name.contains("update") || name.contains("patch") {
                return "Moving \(title ?? "a meeting")"
            }
            if name.contains("create") || name.contains("insert") { return "Scheduling \(title ?? "a meeting")" }
            if name.contains("read") || name.contains("get") { return "Looking at a meeting" }
            return raw(["start", "time_min"]).flatMap(day).map { "Checking your calendar for \($0)" }
                ?? "Checking your calendar"
        }

        // People, to-dos, jobs
        if name.contains("contact") {
            if name.contains("note") { return "Noting something about a contact" }
            return arg("query").map { "Looking up \($0)" } ?? "Looking up a contact"
        }
        if name == "start_task" { return arg("title").map { "Starting a job: \($0)" } ?? "Starting a job" }
        if name.hasSuffix("_task") && (name.contains("inspect") || name.contains("update") || name.contains("stop")) {
            return "Checking on a job"
        }
        if name.contains("task") || name.contains("todo") {
            if name.contains("add") || name.contains("create") {
                return arg("title").map { "Adding a to-do: \($0)" } ?? "Adding a to-do"
            }
            if name.contains("complete") { return "Ticking off a to-do" }
            return "Checking your to-dos"
        }

        // Web, memory, notes, tools
        if name.contains("search_web") || name.contains("web_search") {
            return arg("query").map { "Searching the web for “\($0)”" } ?? "Searching the web"
        }
        if name.contains("webpage") || name.contains("fetch_url") || name.contains("browse") {
            return raw(["urls", "url", "0"]).flatMap(host).map { "Reading \($0)" } ?? "Reading a page"
        }
        if name.contains("memory") || name.contains("recall") {
            return arg("query").map { "Checking what I know about “\($0)”" } ?? "Checking what I remember"
        }
        if name.contains("remember") { return "Remembering that" }
        if name.hasPrefix("workspace_") {
            return name.contains("create") || name.contains("update") ? "Writing notes" : "Looking through my notes"
        }
        if name.contains("capabilit") || name.contains("skill") { return "Getting the right tools ready" }
        if name.contains("tool_result") { return "Going through the results" }
        if let app { return "Working in \(app)" }
        return nil
    }

    /// Functions that do bookkeeping, not work worth naming.
    private static let ignored: Set<String> = [
        "current_time", "print", "gather", "len", "str", "int", "float", "list", "dict", "sorted",
        "range", "enumerate", "zip", "min", "max", "sum", "set", "any", "all", "isinstance", "type",
        "ask_user", "get", "items", "keys", "values", "append", "join", "split", "strip", "format",
        "lower", "upper", "startswith", "endswith", "replace", "loads", "dumps", "sleep",
    ]

    /// Composio tools are named APP_ACTION; the app part, in the words people use for it.
    private static func composioApp(_ name: String) -> String? {
        guard name == name.uppercased(), let prefix = name.split(separator: "_").first else { return nil }
        let known = [
            "GMAIL": "Gmail", "GOOGLECALENDAR": "Calendar", "GITHUB": "GitHub", "LINEAR": "Linear",
            "NOTION": "Notion", "SLACK": "Slack", "GOOGLEDRIVE": "Drive", "GOOGLEDOCS": "Docs",
            "GOOGLESHEETS": "Sheets", "OUTLOOK": "Outlook",
        ]
        return known[String(prefix)] ?? String(prefix).capitalized
    }

    /// The function calls in a code snippet, with their string arguments: positional ones as
    /// "0", "1" and keyword ones by name. A loose read, not a Python parser: enough for words.
    static func calls(in code: String) -> [(name: String, args: [String: String])] {
        var out: [(String, [String: String])] = []
        let call = /([A-Za-z_][A-Za-z0-9_]*)\s*\(/
        for match in code.matches(of: call) {
            let name = String(match.1)
            // Methods (`result.get(`) are plumbing; only bare function calls are tools.
            let before = code[..<match.range.lowerBound].last
            if before == "." { continue }
            let rest = code[match.range.upperBound...]
            out.append((name, arguments(String(rest.prefix(400)))))
        }
        return out
    }

    /// `query='Dana', limit=5` or `urls=['https://…']` from the text after an opening bracket.
    private static func arguments(_ text: String) -> [String: String] {
        var args: [String: String] = [:]
        let keyword = /([A-Za-z_]+)\s*=\s*\[?\s*(?:'([^']*)'|"([^"]*)")/
        for match in text.matches(of: keyword) {
            args[String(match.1)] = String(match.2 ?? match.3 ?? "")
        }
        if let first = text.firstMatch(of: /^\s*(?:'([^']*)'|"([^"]*)")/) {
            args["0"] = String(first.1 ?? first.2 ?? "")
        }
        return args
    }

    private static func dedupe(_ steps: [String]) -> [String] {
        var seen = Set<String>()
        return steps.filter { seen.insert($0).inserted }
    }

    private static func short(_ text: String) -> String {
        let line = text.split(whereSeparator: \.isNewline).first.map(String.init) ?? text
        return line.count > 36 ? String(line.prefix(34)) + "…" : line
    }

    private static func host(_ url: String) -> String? {
        URL(string: url.trimmingCharacters(in: .whitespaces))?.host()
            .map { $0.hasPrefix("www.") ? String($0.dropFirst(4)) : $0 }
    }

    /// "2026-11-06T00:00:00" reads as "Fri, Nov 6".
    private static func day(_ value: String) -> String? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        guard value.count >= 10, let date = formatter.date(from: String(value.prefix(10))) else { return nil }
        return date.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day())
    }
}
