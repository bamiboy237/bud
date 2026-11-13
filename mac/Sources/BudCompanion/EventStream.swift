import Foundation

/// One event from bud's session log, as the SSE stream sends it.
struct BudEvent: @unchecked Sendable {
    let seq: Int
    let type: String
    let data: [String: Any]
    /// Live events (streamed text) are not stored and carry no reconnect position.
    let live: Bool

    init?(json line: Substring) {
        guard let object = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
              let type = object["type"] as? String
        else { return nil }
        self.type = type
        self.seq = object["seq"] as? Int ?? 0
        self.data = object["data"] as? [String: Any] ?? [:]
        self.live = object["live"] as? Bool ?? false
    }

    func string(_ key: String) -> String { data[key] as? String ?? "" }
}

enum StreamSignal: Sendable {
    case connected
    case event(BudEvent)
    case lost
    /// 401, 404 or 409: reconnecting cannot help (bad token, unknown session, account switched).
    case fatal(Int)
}

/// Follows a session's events forever: reconnects after a second, resuming after the last
/// stored event it saw, so a dropped connection or a server restart loses and repeats nothing.
func followEvents(
    _ connection: BudConnection, session: String, after start: Int,
    deliver: @escaping @Sendable @MainActor (StreamSignal) -> Void
) async {
    var lastSeq = start
    while !Task.isCancelled {
        var request = connection.request("GET", "sessions/\(session)/events")
        request.timeoutInterval = .infinity
        request.setValue(String(lastSeq), forHTTPHeaderField: "Last-Event-ID")
        do {
            let (bytes, response) = try await URLSession.shared.bytes(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if [401, 404, 409].contains(code) {
                await deliver(.fatal(code))
                return
            }
            if (200..<300).contains(code) {
                await deliver(.connected)
                // bud writes each event as one `data:` line of JSON, so a line is a whole event.
                // (`lines` drops the blank lines that end SSE frames; we don't need them.)
                for try await line in bytes.lines {
                    guard line.hasPrefix("data:") else { continue }
                    guard let event = BudEvent(json: line.dropFirst(5).drop(while: { $0 == " " })) else {
                        continue
                    }
                    if !event.live { lastSeq = max(lastSeq, event.seq) }
                    await deliver(.event(event))
                }
            }
        } catch {
            if Task.isCancelled { return }
        }
        await deliver(.lost)
        try? await Task.sleep(for: .seconds(1))
    }
}
