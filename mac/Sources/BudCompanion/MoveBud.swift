import AppKit
import SwiftUI

@MainActor @Observable
final class MoveBudDraft {
    let server = ServerSettingsDraft()
    var source: URL? { didSet { restoreMove() } }
    var workspace: URL?
    var active: Bool { activity != nil }
    private(set) var pending = false
    private(set) var error: String?
    private(set) var activity: String?
    private(set) var progress: String?

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
        if cancel {
            activity = "Canceling move…"
        } else {
            activity = pending ? "Continuing move…" : "Moving Bud…"
        }
        error = nil
        defer {
            activity = nil
            progress = nil
            restoreMove()
        }
        do {
            try RemoteBudServer.save(token: token, for: origin)
            var arguments = ["move-runtime", source.path(), origin.absoluteString]
            if let workspace { arguments.append(workspace.path()) }
            if cancel { arguments.append("--cancel") }
            let serverLabel = origin.host() ?? origin.absoluteString
            let reply = try await Self.runCLI(cli, arguments: arguments, token: token) { stage in
                self.progress = stage.label(server: serverLabel)
            }
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

    enum Stage: String, Decodable {
        case packing
        case uploading
        case starting

        func label(server: String) -> String {
            switch self {
            case .packing: "Packing your files"
            case .uploading: "Uploading to \(server)"
            case .starting: "Starting Bud on \(server)"
            }
        }
    }

    private struct Recovery: Decodable { let destination: String }
    private struct ProgressEvent: Decodable { let stage: Stage }
    struct Reply: Decodable { let state: String }
    private struct MoveError: Error { let message: String }

    static func runCLI(
        _ cli: URL, arguments: [String], token: String, report: (Stage) -> Void
    ) async throws -> Reply {
        let output = Pipe()
        let input = Pipe()
        let process = Process()
        process.executableURL = cli
        process.arguments = arguments
        var environment = ProcessInfo.processInfo.environment
        environment["BUD_MOVE_PROGRESS"] = "1"
        process.environment = environment
        process.standardInput = input
        process.standardOutput = output
        process.standardError = output

        let (statuses, exit) = AsyncStream<Int32>.makeStream()
        process.terminationHandler = { process in
            exit.yield(process.terminationStatus)
            exit.finish()
        }
        try process.run()

        let (chunks, outputStream) = AsyncStream<Data>.makeStream()
        output.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            if chunk.isEmpty {
                handle.readabilityHandler = nil
                outputStream.finish()
            } else {
                outputStream.yield(chunk)
            }
        }
        input.fileHandleForWriting.write(Data("\(token)\n".utf8))
        input.fileHandleForWriting.closeFile()

        let decoder = JSONDecoder()
        var buffer = Data()
        var lastLine = Data()
        for await chunk in chunks {
            buffer.append(chunk)
            while let end = buffer.firstIndex(of: 0x0A) {
                let line = buffer.prefix(upTo: end)
                buffer.removeFirst(line.count + 1)
                if let event = try? decoder.decode(ProgressEvent.self, from: line) {
                    report(event.stage)
                } else if !line.isEmpty {
                    lastLine = line
                }
            }
        }
        if !buffer.isEmpty { lastLine = buffer }
        var termination = statuses.makeAsyncIterator()
        let status = await termination.next()
        guard !lastLine.isEmpty else {
            throw MoveError(message: "Bud couldn’t finish the move.")
        }
        guard status == 0 else {
            throw MoveError(message: String(decoding: lastLine, as: UTF8.self))
        }
        return try decoder.decode(Reply.self, from: lastLine)
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
            if draft.pending {
                Label(
                    draft.active ? "Bud is stopped on this Mac" : "Move paused · Bud is stopped on this Mac",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(Paper.Typography.callout.weight(.semibold))
                .foregroundStyle(Paper.errorInk)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Paper.denyFill, in: RoundedRectangle(cornerRadius: Paper.radiusSM))
            }
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
            if !draft.pending {
                Text("Bud will run on this server instead of your Mac. Your chats, workspace, routines, models, and supported sign-ins move with it.")
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = draft.error {
                Text(error).font(Paper.Typography.callout).foregroundStyle(Paper.errorInk)
            }
            if let activity = draft.activity {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(activity)
                        if let progress = draft.progress {
                            Text(progress)
                                .font(Paper.Typography.callout)
                                .lineLimit(1)
                                .help(progress)
                        }
                    }
                    .foregroundStyle(Paper.inkSecondary)
                }
                .accessibilityElement(children: .combine)
            }
            HStack(spacing: 10) {
                Spacer()
                Button("Close", action: cancel)
                    .keyboardShortcut(.cancelAction)
                    .disabled(draft.active)
                if draft.pending {
                    Button("Cancel move and resume on this Mac") {
                        Task { if await draft.move(cancel: true) { moved() } }
                    }
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
