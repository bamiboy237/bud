import Foundation

/// An earlier message a reply cites: which conversation, which message in it, and its date.
struct Source: Equatable {
    let session: String
    let seq: Int
    /// "Nov 18", or "Nov 18, 2025" from another year.
    let label: String
}

/// A reply cites an earlier message by a short tag, like `[S1]`, which bud chose and the
/// model copied. The run's `sources` say what each tag points to. Here a tag becomes a link
/// you can click; a tag the run doesn't know is dropped, since the model made it up.
enum Citations {
    static let scheme = "bud-source"

    /// Tags as markdown links that open the cited message.
    static func linked(_ text: String, sources: [String: Source]) -> String {
        replacing(text, sources) { source in
            " [\(source.label)](\(scheme)://\(source.session)/\(source.seq))"
        }
    }

    /// Tags as plain dates, for copying a reply somewhere links don't open.
    static func plain(_ text: String, sources: [String: Source]) -> String {
        replacing(text, sources) { " (\($0.label))" }
    }

    /// The `sources` map of a run or transcript turn.
    static func sources(_ raw: Any?) -> [String: Source] {
        guard let raw = raw as? [String: Any] else { return [:] }
        return raw.compactMapValues { value in
            guard let value = value as? [String: Any],
                  let session = value["session_id"] as? String,
                  let seq = value["seq"] as? Int,
                  let label = value["label"] as? String
            else { return nil }
            return Source(session: session, seq: seq, label: label)
        }
    }

    /// The conversation and message a source link opens, or nil for any other link.
    static func target(_ url: URL) -> (session: String, seq: Int)? {
        guard url.scheme == scheme, let session = url.host(), !session.isEmpty,
              let seq = Int(url.lastPathComponent)
        else { return nil }
        return (session, seq)
    }

    private static func replacing(
        _ text: String, _ sources: [String: Source], with render: (Source) -> String
    ) -> String {
        guard text.contains("[S") else { return text }
        return text.replacing(#/ ?\[(S\d{1,2})\]/#) { match in
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

/// A cited message from another conversation, with the exchange around it.
struct SourcePanel: Equatable {
    let session: String
    let seq: Int
    /// "Tuesday, Nov 18, 2026"; empty while loading.
    var date = ""
    var turns: [Turn] = []
    var problem: String?

    var loading: Bool { turns.isEmpty && problem == nil }

    init(session: String, seq: Int, problem: String? = nil) {
        self.session = session
        self.seq = seq
        self.problem = problem
    }

    init?(_ raw: [String: Any]) {
        guard let session = raw["session_id"] as? String, let seq = raw["seq"] as? Int else {
            return nil
        }
        self.init(session: session, seq: seq)
        if let day = (raw["date"] as? String).flatMap(Self.day.date(from:)) {
            date = day.formatted(.dateTime.weekday(.wide).month(.abbreviated).day().year())
        }
        turns = (raw["turns"] as? [[String: Any]] ?? []).compactMap { turn in
            guard let text = turn["text"] as? String, !text.isEmpty else { return nil }
            return Turn(
                fromOwner: turn["role"] as? String == "user", text: text, seq: turn["seq"] as? Int,
                sources: Citations.sources(turn["sources"]))
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
