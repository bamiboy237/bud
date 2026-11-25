import Foundation

struct LocalBudService: Equatable, Sendable {
    let home: URL
    let port: Int

    private static let label = "app.bud.server"

    var url: String { "http://127.0.0.1:\(port)" }

    static func installed(in userHome: URL = FileManager.default.homeDirectoryForCurrentUser)
        -> LocalBudService?
    {
        guard let data = try? Data(contentsOf: plist(in: userHome)),
              let payload = try? PropertyListSerialization.propertyList(from: data, format: nil),
              let config = payload as? [String: Any],
              config["Label"] as? String == label,
              let home = config["WorkingDirectory"] as? String, home.hasPrefix("/"),
              let args = config["ProgramArguments"] as? [String],
              let key = args.firstIndex(of: "--port"),
              let value = args.dropFirst(key + 1).first,
              let port = Int(value), (1...65535).contains(port)
        else { return nil }
        return LocalBudService(home: URL(filePath: home), port: port)
    }

    private static func plist(in userHome: URL) -> URL {
        userHome.appending(path: "Library/LaunchAgents/\(label).plist")
    }

    static func start(
        userHome: URL = FileManager.default.homeDirectoryForCurrentUser,
        uid: uid_t = getuid(),
        run: ([String]) async throws -> Int32 = launchctl
    ) async throws {
        let domain = "gui/\(uid)"
        if try await run(["kickstart", "\(domain)/\(label)"]) == 0 { return }
        try Task.checkCancellation()
        if try await run(["bootstrap", domain, plist(in: userHome).path()]) == 0 { return }
        throw ConnectionError.localStartupFailed
    }

    private static func launchctl(_ arguments: [String]) async throws -> Int32 {
        try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = URL(filePath: "/bin/launchctl")
            process.arguments = arguments
            process.standardOutput = FileHandle.nullDevice
            process.standardError = FileHandle.nullDevice
            process.terminationHandler = { continuation.resume(returning: $0.terminationStatus) }
            do {
                try process.run()
            } catch {
                continuation.resume(throwing: ConnectionError.localStartupFailed)
            }
        }
    }
}
