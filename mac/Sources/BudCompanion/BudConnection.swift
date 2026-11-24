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
    typealias Transport = @Sendable (URLRequest) async throws -> (Data, URLResponse)

    let base: URL
    let token: String
    /// The account scope from the handshake. Sent on every later request, so a server that
    /// switched accounts refuses old clients (409) instead of mixing approvals.
    var scope: String = ""
    private let transport: Transport

    init(settings: BudSettings) throws {
        guard let base = URL(string: settings.url), Self.safeOrigin(base) else {
            throw ConnectionError.badURL
        }
        self.base = base
        self.token = try Self.ownerToken(home: settings.home)
        self.transport = { try await budSession.data(for: $0) }
    }

    init(
        base: URL,
        token: String,
        scope: String = "",
        transport: @escaping Transport = { try await budSession.data(for: $0) }
    ) {
        self.base = base
        self.token = token
        self.scope = scope
        self.transport = transport
    }

    /// Never send the owner secret over plaintext HTTP to another machine.
    static func safeOrigin(_ url: URL) -> Bool {
        let loopback = ["127.0.0.1", "localhost", "::1"].contains(url.host() ?? "")
        let plain = url.path().isEmpty || url.path() == "/"
        return (url.scheme == "https" || (url.scheme == "http" && loopback))
            && plain && url.user() == nil && url.query() == nil
    }

    static func resolvedHomeURL(
        home: String,
        currentDirectory: URL = URL(filePath: FileManager.default.currentDirectoryPath),
        userHome: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> URL {
        let files = FileManager.default
        if !home.isEmpty {
            let expanded = if home == "~" {
                userHome.path()
            } else if home.hasPrefix("~/") {
                userHome.appending(path: String(home.dropFirst(2))).path()
            } else {
                home
            }
            return URL(filePath: expanded)
        }
        if files.fileExists(atPath: currentDirectory.appending(path: ".env").path())
            || files.fileExists(atPath: currentDirectory.appending(path: "data/auth/owner.token").path())
        {
            return currentDirectory
        }
        let installed = userHome.appending(path: ".bud")
        if files.fileExists(atPath: installed.appending(path: "config.env").path())
            || files.fileExists(atPath: installed.appending(path: "data/auth/owner.token").path())
        {
            return installed
        }
        return currentDirectory
    }

    static func ownerToken(
        home: String,
        currentDirectory: URL = URL(filePath: FileManager.default.currentDirectoryPath),
        userHome: URL = FileManager.default.homeDirectoryForCurrentUser,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) throws -> String {
        let homeURL = resolvedHomeURL(
            home: home, currentDirectory: currentDirectory, userHome: userHome)
        let configValues = Self.dotenv(homeURL.appending(path: "config.env"))
        let envValues = Self.dotenv(homeURL.appending(path: ".env"))
        let dotenv = envValues.merging(configValues) { _, configValue in configValue }
        if let token = environment["OWNER_TOKEN"] ?? dotenv["OWNER_TOKEN"], !token.isEmpty {
            guard valid(token) else {
                throw ConnectionError.noToken("OWNER_TOKEN must be a 43 to 256 character base64url secret.")
            }
            return token
        }
        let dataDir = environment["DATA_DIR"] ?? dotenv["DATA_DIR"] ?? "data"
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

    func request(
        _ method: String, _ path: String, json: [String: Any]? = nil,
        queryItems: [URLQueryItem] = []
    ) -> URLRequest {
        var url = base.appending(path: path)
        if !queryItems.isEmpty { url = url.appending(queryItems: queryItems) }
        var request = URLRequest(url: url)
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

    func send(_ urlRequest: URLRequest) async throws -> [String: Any] {
        let data = try await checkedData(urlRequest)
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }

    func send(
        _ method: String, _ path: String, json: [String: Any]? = nil,
        queryItems: [URLQueryItem] = []
    ) async throws -> [String: Any] {
        try await send(request(method, path, json: json, queryItems: queryItems))
    }

    func getBytes(_ path: String, queryItems: [URLQueryItem]) async throws -> Data {
        try await checkedData(request("GET", path, queryItems: queryItems))
    }

    func postMessage(
        _ text: String, id: String, session: String, attachments: [DraftAttachment]
    ) async throws {
        for file in attachments {
            let reference = file.reference
            var upload = request("PUT", reference.path(in: session), queryItems: reference.query)
            upload.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
            upload.timeoutInterval = 60
            upload.httpBody = file.data
            _ = try await checkedData(upload)
        }
        var body: [String: Any] = ["text": text, "message_id": id, "origin": Self.origin]
        if !attachments.isEmpty { body["attachments"] = attachments.map { $0.reference.fields } }
        _ = try await send("POST", "sessions/\(session)/messages", json: body)
    }

    private func checkedData(_ request: URLRequest) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await transport(request)
        } catch {
            throw ConnectionError.unreachable
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else {
            let detail = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw ConnectionError.http(code, detail?["detail"].map { "\($0)" } ?? "request failed")
        }
        return data
    }

    /// Who sent a message: routing metadata the server records, not a permission.
    static var origin: [String: Any] { ["channel": "desktop", "client_id": "mac-companion"] }
}
