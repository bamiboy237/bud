import Foundation

struct TurnProvenance: Equatable {
    struct WebSource: Decodable, Equatable, Identifiable {
        enum Status: String, Decodable { case read, found }

        let title: String
        let url: String
        let domain: String
        let excerpt: String
        let status: Status
        var id: String { url }

        var link: URL? {
            guard let link = URL(string: url),
                  ["https", "http"].contains(link.scheme?.lowercased() ?? ""),
                  link.host() != nil, link.user() == nil else { return nil }
            return link
        }
    }

    struct ApprovalReceipt: Decodable, Equatable, Identifiable {
        enum Status: String, Decodable { case confirmed, denied, unknown }

        let approvalId: String
        let label: String
        let decision: String
        let decidedAt: String
        let status: Status
        var id: String { approvalId }

        var summary: String {
            switch status {
            case .confirmed:
                if let time = parseBudTimestamp(decidedAt) {
                    return "Approved \(time.formatted(date: .omitted, time: .shortened))"
                }
                return "Approved"
            case .denied: return "Denied"
            case .unknown: return "Outcome unknown"
            }
        }
    }

    struct Memory: Decodable, Equatable, Identifiable {
        let id: String
        let text: String
        let scope: String
        let source: String
    }

    struct BrowserFrame: Decodable, Equatable, Identifiable {
        let frameId: String
        let url: String
        let title: String
        let capturedAt: String
        var id: String { frameId }
        var domain: String { URL(string: url)?.host() ?? "browser page" }
    }

    let webSources: [WebSource]
    let approvalReceipts: [ApprovalReceipt]
    let memoryUsed: [Memory]
    let browserFrames: [BrowserFrame]

    var isEmpty: Bool {
        webSources.isEmpty && approvalReceipts.isEmpty && memoryUsed.isEmpty && browserFrames.isEmpty
    }

    init(_ payload: [String: Any] = [:]) {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        func records<Record: Decodable>(_ raw: Any?) -> [Record] {
            (raw as? [[String: Any]] ?? []).compactMap {
                guard let data = try? JSONSerialization.data(withJSONObject: $0) else { return nil }
                return try? decoder.decode(Record.self, from: data)
            }
        }

        let web = payload["web_sources"] as? [[String: Any]]
        webSources = records(web ?? (payload["sources"] as? [[String: Any]]))
        approvalReceipts = records(payload["approval_receipts"])
        memoryUsed = records(payload["memory_used"])
        browserFrames = records(payload["browser_frames"])
    }
}

func parseBudTimestamp(_ value: String) -> Date? {
    (try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(value))
        ?? (try? Date.ISO8601FormatStyle().parse(value))
}
