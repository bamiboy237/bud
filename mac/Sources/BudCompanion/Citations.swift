import Foundation

enum SourceKind: Equatable {
    case turn(session: String, seq: Int)
    case workspace(path: String)
}

/// An earlier message or workspace file a reply cites, with its short display label.
struct Source: Equatable {
    let kind: SourceKind
    /// "Nov 18", "Nov 18, 2025", or a workspace relative path like "reports/weekly.md".
    let label: String
}

enum SourceTarget: Hashable {
    case turn(session: String, seq: Int)
    case workspace(path: String)
}

/// A reply cites an earlier message or workspace file by a short tag, like `[S1]`, which bud
/// chose and the model copied. The run's `sources` say what each tag points to. Here a tag becomes
/// a link you can click; a tag the run doesn't know is dropped, since the model made it up.
enum Citations {
    static let scheme = "bud-source"

    /// Tags as markdown links that open the cited message or workspace file.
    static func linked(_ text: String, sources: [String: Source]) -> String {
        replacing(text, sources) { source in
            switch source.kind {
            case .turn(let session, let seq):
                return " [\(source.label)](\(scheme)://\(session)/\(seq))"
            case .workspace(let path):
                var components = URLComponents()
                components.scheme = scheme
                components.host = "workspace"
                components.queryItems = [URLQueryItem(name: "path", value: path)]
                let destination = (components.string ?? "\(scheme)://workspace")
                    .replacingOccurrences(of: "(", with: "%28")
                    .replacingOccurrences(of: ")", with: "%29")
                return " [\(source.label)](\(destination))"
            }
        }
    }

    /// Tags as plain dates or paths, for copying a reply somewhere links don't open.
    static func plain(_ text: String, sources: [String: Source]) -> String {
        replacing(text, sources) { " (\($0.label))" }
    }

    /// The `sources` map of a run or transcript turn.
    static func sources(_ raw: Any?) -> [String: Source] {
        guard let raw = raw as? [String: Any] else { return [:] }
        return raw.compactMapValues { value in
            guard let value = value as? [String: Any],
                  let label = value["label"] as? String
            else { return nil }
            if value["kind"] as? String == "workspace",
               let path = value["path"] as? String, !path.isEmpty {
                return Source(kind: .workspace(path: path), label: label)
            }
            guard let session = value["session_id"] as? String,
                  let seq = value["seq"] as? Int
            else { return nil }
            return Source(kind: .turn(session: session, seq: seq), label: label)
        }
    }

    /// The conversation message or workspace file a source link opens, or nil for any other link.
    static func target(_ url: URL) -> SourceTarget? {
        guard url.scheme == scheme, let host = url.host(), !host.isEmpty else { return nil }
        if host == "workspace" {
            let path = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?
                .first(where: { $0.name == "path" })?
                .value
            guard let path, !path.isEmpty else { return nil }
            return .workspace(path: path)
        }
        guard let seq = Int(url.lastPathComponent) else { return nil }
        return .turn(session: host, seq: seq)
    }

    private static func replacing(
        _ text: String, _ sources: [String: Source], with render: (Source) -> String
    ) -> String {
        guard text.contains("[S") else { return text }
        return text.replacing(#/ ?\[(S\d{1,6})\]/#) { match in
            sources[String(match.output.1)].map(render) ?? ""
        }
    }
}

/// Where the conversation window scrolls when a source link points into it.
enum Focus: Equatable {
    case turn(UUID)
    /// The latest reply, which isn't in `turns` yet.
    case latest
}

/// A cited message or workspace file opened beside the chat.
struct SourcePanel: Equatable {
    let target: SourceTarget
    /// "Tuesday, Nov 18, 2026"; empty while loading.
    var date = ""
    var turns: [Turn] = []
    var document: String?
    var problem: String?

    var loading: Bool { turns.isEmpty && document == nil && problem == nil }

    var citedTurn: Turn? {
        guard case .turn(_, let seq) = target else { return nil }
        return turns.first { $0.seq == seq }
    }

    init(target: SourceTarget, problem: String? = nil) {
        self.target = target
        self.problem = problem
    }

    init?(_ raw: [String: Any]) {
        let formattedDate = (raw["date"] as? String)
            .flatMap(Self.day.date(from:))
            .map { $0.formatted(.dateTime.weekday(.wide).month(.abbreviated).day().year()) } ?? ""
        if raw["kind"] as? String == "workspace",
           let path = raw["path"] as? String,
           let content = raw["content"] as? String {
            self.init(target: .workspace(path: path))
            date = formattedDate
            document = content
            return
        }
        guard let session = raw["session_id"] as? String, let seq = raw["seq"] as? Int else {
            return nil
        }
        self.init(target: .turn(session: session, seq: seq))
        date = formattedDate
        turns = (raw["turns"] as? [[String: Any]] ?? []).compactMap { turn in
            let files = ChatAttachment.decode(turn["attachments"])
            guard let text = turn["text"] as? String, !text.isEmpty || !files.isEmpty else {
                return nil
            }
            return Turn(
                fromOwner: turn["role"] as? String == "user", text: text, seq: turn["seq"] as? Int,
                sources: Citations.sources(turn["sources"]), attachments: files)
        }
    }

    private static let day: DateFormatter = {
        let format = DateFormatter()
        format.locale = Locale(identifier: "en_US_POSIX")
        format.timeZone = .current
        format.dateFormat = "yyyy-MM-dd"
        return format
    }()
}
