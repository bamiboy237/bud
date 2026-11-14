import Foundation

/// Where bud runs and how to authenticate, found the same way the TUI finds it:
/// environment first, then the bud folder's `.env`, then `DATA_DIR/auth/owner.token`.
struct BudSettings: Sendable {
    static let defaultURL = "http://127.0.0.1:8420"

    var url: String
    /// The bud checkout or install folder; `.env` and relative `DATA_DIR` resolve against it.
    var home: String

    static func load() -> BudSettings {
        let env = ProcessInfo.processInfo.environment
        let defaults = UserDefaults.standard
        return BudSettings(
            url: env["BUD_URL"] ?? defaults.string(forKey: "budURL") ?? defaultURL,
            home: env["BUD_HOME"] ?? defaults.string(forKey: "budHome") ?? ""
        )
    }

    func save() {
        UserDefaults.standard.set(url, forKey: "budURL")
        UserDefaults.standard.set(home, forKey: "budHome")
    }
}

enum ConnectionError: Error, CustomStringConvertible {
    case badURL
    case noToken(String)
    case unreachable
    case http(Int, String)

    var description: String {
        switch self {
        case .badURL:
            "The bud URL must be http on this Mac (127.0.0.1 or localhost) or https."
        case .noToken(let detail):
            detail
        case .unreachable:
            "Can't reach bud. Start it with bud serve, then choose Reconnect."
        case .http(let code, let detail):
            "bud answered \(code): \(detail)"
        }
    }
}

/// An authenticated handle on one bud server.
struct BudConnection: Sendable {
    let base: URL
    let token: String
    /// The account scope from the handshake. Sent on every later request, so a server that
    /// switched accounts refuses old clients (409) instead of mixing approvals.
    var scope: String = ""

    init(settings: BudSettings) throws {
        guard let base = URL(string: settings.url), Self.safeOrigin(base) else {
            throw ConnectionError.badURL
        }
        self.base = base
        self.token = try Self.ownerToken(home: settings.home)
    }

    /// Never send the owner secret over plaintext HTTP to another machine.
    static func safeOrigin(_ url: URL) -> Bool {
        let loopback = ["127.0.0.1", "localhost", "::1"].contains(url.host() ?? "")
        let plain = url.path().isEmpty || url.path() == "/"
        return (url.scheme == "https" || (url.scheme == "http" && loopback))
            && plain && url.user() == nil && url.query() == nil
    }

    static func ownerToken(home: String) throws -> String {
        let env = ProcessInfo.processInfo.environment
        let homeURL = URL(filePath: home.isEmpty ? FileManager.default.currentDirectoryPath : home)
        let dotenv = Self.dotenv(homeURL.appending(path: ".env"))
        if let token = env["OWNER_TOKEN"] ?? dotenv["OWNER_TOKEN"], !token.isEmpty {
            guard valid(token) else {
                throw ConnectionError.noToken("OWNER_TOKEN must be a 43 to 256 character base64url secret.")
            }
            return token
        }
        let dataDir = env["DATA_DIR"] ?? dotenv["DATA_DIR"] ?? "data"
        let dataURL = dataDir.hasPrefix("/") ? URL(filePath: dataDir) : homeURL.appending(path: dataDir)
        let path = dataURL.appending(path: "auth/owner.token")
        guard let raw = try? String(contentsOf: path, encoding: .utf8) else {
            throw ConnectionError.noToken(
                "No owner token at \(path.path()). Set the bud folder in Settings, or start `bud serve` once.")
        }
        let token = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard valid(token) else { throw ConnectionError.noToken("The owner token file is invalid.") }
        return token
    }

    private static func valid(_ token: String) -> Bool {
        (43...256).contains(token.count)
            && token.unicodeScalars.allSatisfy {
                CharacterSet.alphanumerics.contains($0) && $0.isASCII || $0 == "-" || $0 == "_"
            }
    }

    private static func dotenv(_ url: URL) -> [String: String] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [:] }
        var values: [String: String] = [:]
        for line in text.split(whereSeparator: \.isNewline) {
            let line = line.trimmingCharacters(in: .whitespaces)
            guard !line.hasPrefix("#"), let eq = line.firstIndex(of: "=") else { continue }
            let key = line[..<eq].trimmingCharacters(in: .whitespaces).uppercased()
            values[key] = line[line.index(after: eq)...]
                .trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        }
        return values
    }

    // MARK: Requests

    func request(_ method: String, _ path: String, json: [String: Any]? = nil) -> URLRequest {
        var request = URLRequest(url: base.appending(path: path))
        request.httpMethod = method
        request.timeoutInterval = 10
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if !scope.isEmpty { request.setValue(scope, forHTTPHeaderField: "x-bud-scope") }
        if let json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: json)
        }
        return request
    }

    func send(_ method: String, _ path: String, json: [String: Any]? = nil) async throws -> [String: Any] {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await budSession.data(for: request(method, path, json: json))
        } catch {
            throw ConnectionError.unreachable
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        guard (200..<300).contains(code) else {
            throw ConnectionError.http(code, body["detail"].map { "\($0)" } ?? "request failed")
        }
        return body
    }

    /// Who sent a message: routing metadata the server records, not a permission.
    static var origin: [String: Any] { ["channel": "desktop", "client_id": "mac-companion"] }
}
