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

/// No cookies, cache or credential store: the owner token is the only credential, sent per request.
let budSession = URLSession(configuration: .ephemeral, delegate: BudSessionDelegate(), delegateQueue: nil)

private final class BudSessionDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(
        _ session: URLSession, task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest
    ) async -> URLRequest? {
        nil
    }
}

/// Follows a session's events forever: reconnects with exponential backoff and bounded jitter,
/// resuming after the last stored event it saw, so a dropped connection or a server restart
/// loses and repeats nothing.
func followEvents(
    _ connection: BudConnection,
    session: String,
    after start: Int,
    initialBackoff: ReconnectBackoff = ReconnectBackoff(),
    sleep: @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) },
    deliver: @escaping @Sendable @MainActor (StreamSignal) -> Void
) async {
    var lastSeq = start
    var backoff = initialBackoff
    while !Task.isCancelled {
        var request = connection.request("GET", "sessions/\(session)/events")
        // bud sends a keep-alive every 15 s, so 45 s of silence means a dead connection.
        request.timeoutInterval = 45
        request.setValue(String(lastSeq), forHTTPHeaderField: "Last-Event-ID")
        do {
            let (bytes, response) = try await budSession.bytes(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if [401, 404, 409].contains(code) {
                await deliver(.fatal(code))
                return
            }
            if (200..<300).contains(code) {
                backoff.reset()
                await deliver(.connected)
                // bud writes each event as one `data:` line of JSON, so a line is a whole event.
                // (`lines` drops the blank lines that end SSE frames; we don't need them.)
                for try await line in bytes.lines {
                    guard line.hasPrefix("data:") else { continue }
                    guard let event = BudEvent(json: line.dropFirst(5).drop(while: { $0 == " " })) else {
                        continue
                    }
                    if !event.live { lastSeq = max(lastSeq, event.seq) }
                    backoff.reset()
                    await deliver(.event(event))
                }
            }
        } catch {
            if Task.isCancelled { return }
        }
        await deliver(.lost)
        let delay = backoff.nextDelay()
        try? await sleep(delay)
    }
}
