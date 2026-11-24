import Foundation

/// Parsed semantic version supporting optional prerelease suffixes (`0.2.0-rc1`).
struct SemanticVersion: Comparable, Equatable, Sendable {
    let numbers: [Int]
    let prerelease: String?

    init?(_ raw: String) {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        let noPrefix = trimmed.hasPrefix("v") || trimmed.hasPrefix("V")
            ? String(trimmed.dropFirst())
            : trimmed
        let pieces = noPrefix.split(separator: "-", maxSplits: 1, omittingEmptySubsequences: false)
        let coreParts = pieces[0].split(separator: ".", omittingEmptySubsequences: false)
        guard !coreParts.isEmpty else { return nil }
        var parsed: [Int] = []
        for part in coreParts {
            guard let num = Int(part), num >= 0 else { return nil }
            parsed.append(num)
        }
        self.numbers = parsed
        self.prerelease = pieces.count > 1 && !pieces[1].isEmpty ? String(pieces[1]) : nil
    }

    static func < (lhs: SemanticVersion, rhs: SemanticVersion) -> Bool {
        let count = max(lhs.numbers.count, rhs.numbers.count)
        for index in 0..<count {
            let l = index < lhs.numbers.count ? lhs.numbers[index] : 0
            let r = index < rhs.numbers.count ? rhs.numbers[index] : 0
            if l != r { return l < r }
        }
        switch (lhs.prerelease, rhs.prerelease) {
        case (.some, .none): return true
        case (.none, .some): return false
        case (.some(let l), .some(let r)): return l < r
        case (.none, .none): return false
        }
    }
}

enum ReleaseCheckState: Equatable, Sendable {
    case idle
    case checking
    case upToDate(current: String)
    case available(version: String, releaseURL: URL?)
    case updating(version: String)
    case error(String)
}

@MainActor @Observable
final class AppUpdater {
    static let shared = AppUpdater()
    static let latestReleaseURL = URL(
        string: "https://api.github.com/repos/bamiboy237/bud/releases/latest")!

    typealias Fetcher = @Sendable (URLRequest) async throws -> (Data, URLResponse)

    private(set) var state: ReleaseCheckState = .idle
    private let fetcher: Fetcher

    init(fetcher: @escaping Fetcher = { try await URLSession.shared.data(for: $0) }) {
        self.fetcher = fetcher
    }

    nonisolated static func isDevBuild(bundle: Bundle = .main) -> Bool {
        (bundle.bundleIdentifier ?? "").hasSuffix(".dev")
    }

    nonisolated static func currentAppVersion(bundle: Bundle = .main) -> String {
        bundle.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0"
    }

    nonisolated static func installedCLIPath(
        userHome: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> URL? {
        let budHome = userHome.appending(path: ".bud")
        let candidates = [
            budHome.appending(path: "releases/current/bin/bud"),
            budHome.appending(path: "bin/bud"),
        ]
        let files = FileManager.default
        return candidates.first { files.isExecutableFile(atPath: $0.path()) }
    }

    nonisolated static func parseLatestReleaseJSON(_ data: Data) -> (version: String, url: URL?)? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rawTag = object["tag_name"] as? String,
              let parsed = SemanticVersion(rawTag)
        else {
            return nil
        }
        let clean = parsed.numbers.map(String.init).joined(separator: ".")
            + (parsed.prerelease.map { "-\($0)" } ?? "")
        let url = (object["html_url"] as? String).flatMap(URL.init(string:))
        return (clean, url)
    }

    func checkForUpdates(bundle: Bundle = .main) async {
        state = .checking
        let current = Self.currentAppVersion(bundle: bundle)
        var request = URLRequest(url: Self.latestReleaseURL)
        request.timeoutInterval = 15
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("bud-companion/\(current)", forHTTPHeaderField: "User-Agent")

        do {
            let (data, response) = try await fetcher(request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if code == 404 {
                state = .upToDate(current: current)
                return
            }
            guard (200..<300).contains(code) else {
                state = .error("Update check failed (HTTP \(code)).")
                return
            }
            guard let release = Self.parseLatestReleaseJSON(data),
                  let currentSemver = SemanticVersion(current),
                  let candidateSemver = SemanticVersion(release.version)
            else {
                state = .error("Could not read release metadata.")
                return
            }
            if candidateSemver > currentSemver {
                state = .available(version: release.version, releaseURL: release.url)
            } else {
                state = .upToDate(current: current)
            }
        } catch {
            state = .error("Could not reach GitHub Releases.")
        }
    }

    func installUpdate(
        userHome: URL = FileManager.default.homeDirectoryForCurrentUser
    ) {
        let targetVersion: String = if case .available(let version, _) = state {
            version
        } else {
            "latest"
        }
        guard let cli = Self.installedCLIPath(userHome: userHome) else {
            state = .error("Install Bud with `bud install` first to enable in-app updates.")
            return
        }
        let budHome = userHome.appending(path: ".bud")
        let logsDir = budHome.appending(path: "logs")
        try? FileManager.default.createDirectory(
            at: logsDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let logURL = logsDir.appending(path: "updater.log")
        FileManager.default.createFile(atPath: logURL.path(), contents: nil)

        let process = Process()
        process.executableURL = cli
        process.arguments = ["update", "--yes"]
        var env = ProcessInfo.processInfo.environment
        env["BUD_HOME"] = budHome.path()
        let extraPaths = [
            userHome.appending(path: ".local/bin").path(),
            userHome.appending(path: ".cargo/bin").path(),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
        env["PATH"] = (extraPaths + [env["PATH"] ?? ""]).filter { !$0.isEmpty }.joined(separator: ":")
        process.environment = env
        process.standardInput = FileHandle.nullDevice
        if let logHandle = try? FileHandle(forWritingTo: logURL) {
            _ = try? logHandle.seekToEnd()
            process.standardOutput = logHandle
            process.standardError = logHandle
        }
        do {
            state = .updating(version: targetVersion)
            try process.run()
        } catch {
            state = .error("Failed to launch updater: \(error.localizedDescription)")
        }
    }
}
