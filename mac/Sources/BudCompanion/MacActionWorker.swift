import AppKit
import CoreGraphics
import Foundation

@MainActor @Observable
final class MacActionWorker {
    static let shared = MacActionWorker()
    private static let path = "desktop-worker/mac-actions"
    private static let purpose = "mac-worker"
    private static let defaultsKey = "pairedMacIDs"

    private(set) var localID: String?
    @ObservationIgnored private var task: Task<Void, Never>?

    func start() {
        task?.cancel()
        task = nil
        localID = nil
        guard let origin = RemoteBudServer.origin(BudSettings.load().url),
              let id = savedID(origin),
              let token = try? RemoteBudServer.token(for: origin, purpose: Self.purpose)
        else { return }
        localID = id
        let connection = BudConnection(base: origin, token: token)
        task = Task {
            while !Task.isCancelled {
                do {
                    let work = try await connection.send("POST", Self.path)
                    try Task.checkCancellation()
                    if let id = work["id"] as? String, let action = work["action"] as? String,
                       let args = work["args"] as? [String] {
                        let reply = await MacAppAction.execute(action, args: args)
                        _ = try await connection.send("PUT", Self.path, json: [
                            "id": id, "state": reply.state, "text": reply.text,
                        ])
                    }
                } catch ConnectionError.http(let status, _) where status == 401 || status == 403 {
                    if !Task.isCancelled { forget(origin) }
                    return
                } catch {
                    // Claimed actions are never redelivered. The server reports an unknown
                    // result if this Mac could not send its receipt.
                }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func pair() async throws {
        guard let origin = RemoteBudServer.origin(BudSettings.load().url) else {
            throw ConnectionError.badURL
        }
        let raw = Host.current().localizedName ?? "This Mac"
        let label = String(raw.filter { $0.isLetter || $0.isNumber || " .()-".contains($0) }.prefix(48))
        let result = try await Companion.shared.sendOnboardingRequest(
            "POST", "macs", json: ["label": label.isEmpty ? "This Mac" : label])
        guard let id = result["id"] as? String, let token = result["token"] as? String,
              BudConnection.valid(token) else { throw ConnectionError.notBudServer }
        do {
            try RemoteBudServer.save(token: token, for: origin, purpose: Self.purpose)
        } catch {
            _ = try? await Companion.shared.sendOnboardingRequest("DELETE", "desktops/\(id)")
            throw error
        }
        remember(id, origin: origin)
        start()
    }

    func revoke(_ id: String) async throws {
        _ = try await Companion.shared.sendOnboardingRequest("DELETE", "desktops/\(id)")
        if id == localID, let origin = RemoteBudServer.origin(BudSettings.load().url) {
            task?.cancel()
            task = nil
            forget(origin)
        }
    }

    private func savedID(_ origin: URL) -> String? {
        (UserDefaults.standard.dictionary(forKey: Self.defaultsKey) as? [String: String])?[
            origin.absoluteString]
    }

    private func remember(_ id: String?, origin: URL) {
        var ids = UserDefaults.standard.dictionary(forKey: Self.defaultsKey) as? [String: String] ?? [:]
        ids[origin.absoluteString] = id
        UserDefaults.standard.set(ids, forKey: Self.defaultsKey)
    }

    private func forget(_ origin: URL) {
        remember(nil, origin: origin)
        RemoteBudServer.removeToken(for: origin, purpose: Self.purpose)
        localID = nil
    }
}

enum MacAppAction {
    struct Reply: Sendable {
        let state: String
        let text: String
    }

    static func execute(_ action: String, args: [String]) async -> Reply {
        await Task.detached {
            let session = CGSessionCopyCurrentDictionary() as? [String: Any]
            guard session?[kCGSessionOnConsoleKey as String] as? Bool == true,
                  session?["CGSSessionScreenIsLocked"] as? Bool != true else {
                return Reply(state: "failed", text: "Unlock the Mac and try again.")
            }
            guard let directory = Bundle.main.resourceURL?.appending(path: "MacActions"),
                  let launch = command(action, args: args, directory: directory) else {
                return Reply(state: "failed", text: "Bud does not support that Mac app action.")
            }
            return perform(executable: launch.0, arguments: launch.1)
        }.value
    }

    static func command(_ action: String, args: [String], directory: URL) -> (String, [String])? {
        guard !args.contains(where: { $0.utf8.contains(0) }) else { return nil }
        switch action {
        case "open_app":
            guard args.count == 1,
                  let input = try? Data(contentsOf: directory.appending(path: "apps.json")),
                  let allowed = try? JSONDecoder().decode([String].self, from: input),
                  allowed.contains(args[0]) else { return nil }
            return ("/usr/bin/open", ["-a", args[0]])
        case "music_now_playing":
            guard args.isEmpty else { return nil }
        case "music_control":
            guard args.count == 1, ["play", "pause", "next", "previous"].contains(args[0]) else { return nil }
        case "notes_list", "reminders_list":
            guard args.count == 1, let count = Int(args[0]), (1...100).contains(count) else { return nil }
        case "notes_read":
            guard args.count == 1, (1...200).contains(args[0].unicodeScalars.count) else { return nil }
        case "notes_create":
            guard args.count == 2, (1...200).contains(args[0].unicodeScalars.count),
                  args[1].unicodeScalars.count <= 20_000 else { return nil }
            let title = escape(args[0])
            let lines = args[1].components(separatedBy: .newlines).map {
                "<div>\($0.isEmpty ? "<br>" : escape($0))</div>"
            }.joined()
            return script(action, ["<div><h1>\(title)</h1></div>\(lines)"], directory)
        case "reminders_add":
            guard args.count == 1, (1...300).contains(args[0].unicodeScalars.count) else { return nil }
        default:
            return nil
        }
        return script(action, args, directory)
    }

    private static func escape(_ value: String) -> String {
        value.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "'", with: "&#x27;")
    }

    private static func script(_ action: String, _ args: [String], _ directory: URL) -> (String, [String]) {
        ("/usr/bin/osascript", [directory.appending(path: "\(action).applescript").path()] + args)
    }

    private static func perform(executable: String, arguments: [String]) -> Reply {
        let scratch = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: scratch) }
        do {
            try FileManager.default.createDirectory(
                at: scratch, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            let output = scratch.appending(path: "output")
            FileManager.default.createFile(atPath: output.path(), contents: nil)
            let handle = try FileHandle(forUpdating: output)
            defer { try? handle.close() }
            let process = Process()
            process.executableURL = URL(filePath: executable)
            process.arguments = arguments
            process.standardOutput = handle
            process.standardError = handle
            try process.run()
            let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
            DispatchQueue.global().asyncAfter(deadline: .now() + 32, execute: timeout)
            process.waitUntilExit()
            timeout.cancel()
            try handle.seek(toOffset: 0)
            let text = String(decoding: try handle.read(upToCount: 32_000) ?? Data(), as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if process.terminationReason != .exit {
                return Reply(state: "uncertain", text: "The app did not confirm the action.")
            }
            if process.terminationStatus == 0 { return Reply(state: "ok", text: text) }
            if text.contains("-1743") || text.contains("-1744") {
                return Reply(state: "failed", text: "Allow Bud in System Settings → Privacy & Security → Automation.")
            }
            return Reply(state: "uncertain", text: "The app did not confirm the action.")
        } catch {
            return Reply(state: "failed", text: "Bud could not start the Mac action.")
        }
    }
}
