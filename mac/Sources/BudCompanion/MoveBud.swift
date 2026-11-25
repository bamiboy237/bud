import AppKit
import SwiftUI

@MainActor @Observable
final class MoveBudDraft {
    let server = ServerSettingsDraft()
    var source: URL? { didSet { restoreMove() } }
    var workspace: URL?
    private(set) var active = false
    private(set) var pending = false
    private(set) var error: String?

    var sourceName: String {
        guard let source else { return "No folder selected" }
        return source == LocalBudService.installed()?.home ? "This Mac" : source.lastPathComponent
    }

    var canMove: Bool {
        source != nil && !server.address.isEmpty && !server.token.isEmpty && !active
    }

    func chooseFolder(workspace: Bool = false) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.prompt = "Choose"
        panel.message = workspace ? "Choose the workspace with your files." : "Choose your Bud folder."
        guard panel.runModal() == .OK, let folder = panel.url else { return }
        if workspace {
            self.workspace = folder
        } else {
            self.workspace = nil
            source = folder
        }
        error = nil
    }

    private func restoreMove() {
        pending = false
        guard let source else { return }
        for data in [source.appending(path: "data"), source] {
            let fence = data.appending(path: ".runtime-move-source.json")
            guard let bytes = try? Data(contentsOf: fence),
                  let record = try? JSONDecoder().decode(Recovery.self, from: bytes),
                  let origin = RemoteBudServer.origin(record.destination) else { continue }
            pending = true
            if server.address != origin.absoluteString || server.token.isEmpty {
                server.address = origin.absoluteString
                server.token = (try? RemoteBudServer.token(for: origin)) ?? ""
            }
            return
        }
    }

    func move(cancel: Bool = false) async -> Bool {
        guard canMove, let source else { return false }
        guard let origin = RemoteBudServer.origin(server.address) else {
            error = "Enter the server’s HTTPS address."
            return false
        }
        let token = server.token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard BudConnection.valid(token) else {
            error = "Enter the access token from your Bud server."
            return false
        }
        guard let cli = AppUpdater.installedCLIPath() else {
            error = "Install Bud on this Mac before moving it."
            return false
        }
        active = true
        error = nil
        defer {
            active = false
            restoreMove()
        }
        do {
            try RemoteBudServer.save(token: token, for: origin)
            var arguments = ["move-runtime", source.path(), origin.absoluteString]
            if let workspace { arguments.append(workspace.path()) }
            if cancel { arguments.append("--cancel") }
            let reply = try await runCLI(cli, arguments: arguments, token: token)
            if reply.state == "complete" {
                BudSettings.useRemote(origin)
                return true
            }
        } catch let failure as MoveError {
            error = failure.message
        } catch let failure as ConnectionError {
            error = failure.description
        } catch {
            self.error = "Bud couldn’t finish the move."
        }
        return false
    }

    private struct Recovery: Decodable { let destination: String }
    private struct Reply: Decodable { let state: String }
    private struct MoveError: Error { let message: String }

    private func runCLI(_ cli: URL, arguments: [String], token: String) async throws -> Reply {
        let output = Pipe()
        let input = Pipe()
        let status: Int32 = try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = cli
            process.arguments = arguments
            process.standardInput = input
            process.standardOutput = output
            process.standardError = output
            process.terminationHandler = { continuation.resume(returning: $0.terminationStatus) }
            do {
                try process.run()
                input.fileHandleForWriting.write(Data("\(token)\n".utf8))
                input.fileHandleForWriting.closeFile()
            } catch {
                continuation.resume(throwing: error)
            }
        }
        let text = String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        guard let line = text.split(whereSeparator: \.isNewline).last else {
            throw MoveError(message: "Bud couldn’t finish the move.")
        }
        guard status == 0 else { throw MoveError(message: String(line)) }
        return try JSONDecoder().decode(Reply.self, from: Data(line.utf8))
    }
}

struct MoveBudView: View {
    @Bindable var draft: MoveBudDraft
    let cancel: () -> Void
    let moved: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Move Bud")
                .font(Paper.Typography.h2)
            Group {
                HStack(alignment: .firstTextBaseline, spacing: 14) {
                    Text("From")
                    Spacer()
                    Text(draft.sourceName)
                        .foregroundStyle(Paper.inkSecondary)
                        .lineLimit(1)
                        .help(draft.source?.path() ?? "")
                    Button("Choose folder…") { draft.chooseFolder() }
                        .disabled(draft.pending)
                }
                if let workspace = draft.workspace {
                    Text("Workspace: \(workspace.lastPathComponent)")
                        .foregroundStyle(Paper.inkSecondary)
                }
                Button("Choose a separate workspace…") { draft.chooseFolder(workspace: true) }
                    .buttonStyle(.link)
                    .disabled(draft.source == nil || draft.pending)
                Divider()
                ServerFields(draft: draft.server, lockAddress: draft.pending)
            }
            .disabled(draft.active)
            Text(draft.pending
                 ? "Bud is stopped on this Mac. Continue the move, or cancel to run Bud here again."
                 : "Bud will run on this server instead of your Mac. Your chats, workspace, routines, models, and supported sign-ins move with it.")
                .font(Paper.Typography.callout)
                .foregroundStyle(Paper.inkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if let error = draft.error {
                Text(error).font(Paper.Typography.callout).foregroundStyle(Paper.errorInk)
            }
            HStack(spacing: 10) {
                if draft.active {
                    ProgressView().controlSize(.small)
                    Text("Contacting server…").foregroundStyle(Paper.inkSecondary)
                }
                Spacer()
                Button("Close", action: cancel)
                    .keyboardShortcut(.cancelAction)
                    .disabled(draft.active)
                if draft.pending {
                    Button("Cancel move") { Task { if await draft.move(cancel: true) { moved() } } }
                        .disabled(!draft.canMove)
                }
                Button(draft.pending ? "Continue move" : "Move Bud") {
                    Task { if await draft.move() { moved() } }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!draft.canMove)
            }
        }
        .padding(24)
        .frame(width: 470)
        .background(Paper.canvas)
    }
}
