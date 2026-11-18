import AppKit
import SwiftUI
import Testing
@testable import BudCompanion

@MainActor
struct ApprovalLayoutTests {
    @Test(arguments: [false, true])
    func workspaceActionsFitWithAndWithoutStandingPermission(grantable: Bool) async throws {
        _ = NSApplication.shared
        let companion = Companion()
        companion.apply(snapshot: [
            "last_seq": 1, "pending_approvals": [[
                "approval_id": "write", "tool": "workspace_create", "grantable": grantable,
                "args": ["path": "report.md", "content": "Draft for approval."],
            ]],
        ])
        companion.link = .connected
        let host = NSHostingView(rootView: ApprovalCard(
            companion: companion, approval: try #require(companion.approvals.first),
            onDecide: { _ in }
        ).frame(width: 288).transaction { $0.disablesAnimations = true })
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 288, height: 275),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        for failed in [false, true] {
            if failed {
                companion.completeBatch(.init(approved: [], failures: ["write": "Could not connect. Try again."]),
                                        ids: ["write"])
            }
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(150))
            host.layoutSubtreeIfNeeded()
            #expect(host.fittingSize.height <= 275)
            #expect(host.fittingSize.width == 288)
            if let directory = ProcessInfo.processInfo.environment["BUD_APPROVAL_SNAPSHOTS"] {
                let name = "workspace-\(grantable ? "grantable" : "once")-\(failed ? "retry" : "ready").png"
                try snapshot(host, to: URL(filePath: directory).appending(path: name))
            }
        }
    }

    @Test
    func setupCardFitsThePetPanel() async throws {
        _ = NSApplication.shared
        let companion = Companion()
        companion.apply(snapshot: [
            "last_seq": 1,
            "pending_setup": [[
                "capability": "github", "label": "GitHub", "kind": "url", "status": "waiting",
            ]],
            "transcript": [["role": "assistant", "text": "Connect GitHub to continue."]],
        ])
        companion.link = .connected
        let host = NSHostingView(rootView: PetView(companion: companion, focusPanel: {})
            .transaction { $0.disablesAnimations = true })
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: PetPanel.size),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        host.frame = NSRect(origin: .zero, size: PetPanel.size)
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(150))
        host.layoutSubtreeIfNeeded()
        #expect(host.fittingSize.height <= PetPanel.size.height)
        if let directory = ProcessInfo.processInfo.environment["BUD_APPROVAL_SNAPSHOTS"] {
            try snapshot(host, to: URL(filePath: directory).appending(path: "setup-github.png"))
        }
    }

    @Test
    func longQuestionFitsThePetPanel() async throws {
        _ = NSApplication.shared
        let companion = Companion()
        let options = (1...6).map { ["label": "Option \($0)", "description": String(repeating: "Details. ", count: 20)] }
        let question: [String: Any] = [
            "header": "First", "question": String(repeating: "A long question. ", count: 50),
            "options": options, "multi_select": true,
        ]
        companion.apply(snapshot: [
            "last_seq": 1, "pending_approvals": [[
                "approval_id": "question", "tool": "ask_user", "args": ["questions": [question]],
            ]],
        ])
        companion.link = .connected
        let host = NSHostingView(rootView: PetView(companion: companion, focusPanel: {})
            .transaction { $0.disablesAnimations = true })
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: PetPanel.size),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        host.frame = NSRect(origin: .zero, size: PetPanel.size)
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(150))
        host.layoutSubtreeIfNeeded()
        #expect(host.fittingSize.height <= PetPanel.size.height)
        if let directory = ProcessInfo.processInfo.environment["BUD_APPROVAL_SNAPSHOTS"] {
            try snapshot(host, to: URL(filePath: directory).appending(path: "question-long.png"))
        }
    }

    @Test
    func longEmailFitsThePetPanelWithActionsVisible() async throws {
        _ = NSApplication.shared
        let companion = Companion()
        let body = (1...35).map { "Paragraph \($0). Please check this part of the email before sending." }
            .joined(separator: "\n\n") + "\n\n> Dana's original message\n> Can we move Thursday's sync?"
        let actions: [[String: Any]] = [
            ["approval_id": "send", "run_id": "request", "tool": "send_email",
             "args": ["to": ["dana@brightpath.example"], "subject": "Re: Can we move our Thursday sync?", "body": body]],
            ["approval_id": "archive", "run_id": "request", "tool": "archive_email",
             "args": ["thread_id": "invoice"],
             "preview": ["kind": "email", "status": "available", "subject": "CloudHost invoice",
                         "messages": [["sender": "billing@cloudhost.example", "to": ["alex@example.test"],
                                       "body": "Your invoice is ready.\n\nTotal: $29", "body_format": "plain"],
                                      ["sender": "alex@example.test", "to": ["billing@cloudhost.example"],
                                       "body": "Thanks, received.", "body_format": "plain"]]]],
        ]
        companion.apply(snapshot: [
            "last_seq": 1, "pending_approvals": actions,
            "transcript": [["role": "user", "text": "Reply to Dana, then archive the CloudHost invoice."]],
        ])
        companion.link = .connected
        let host = NSHostingView(rootView: PetView(companion: companion, focusPanel: {})
            .transaction { $0.disablesAnimations = true })
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: PetPanel.size),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        host.frame = NSRect(origin: .zero, size: PetPanel.size)
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(150))
        host.layoutSubtreeIfNeeded()
        #expect(host.fittingSize.height <= PetPanel.size.height)
        if let directory = ProcessInfo.processInfo.environment["BUD_APPROVAL_SNAPSHOTS"] {
            try snapshot(host, to: URL(filePath: directory).appending(path: "approval-long.png"))
            host.appearance = NSAppearance(named: .darkAqua)
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(150))
            try snapshot(host, to: URL(filePath: directory).appending(path: "approval-dark.png"))
            host.appearance = NSAppearance(named: .aqua)
            companion.inputOpen = true
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(150))
            host.layoutSubtreeIfNeeded()
            #expect(host.fittingSize.height <= PetPanel.size.height)
            try snapshot(host, to: URL(filePath: directory).appending(path: "approval-input.png"))
            companion.inputOpen = false
            companion.completeBatch(.init(approved: ["send"], failures: ["archive": "Could not connect. Try again."]),
                                    ids: ["send", "archive"])
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(150))
            host.layoutSubtreeIfNeeded()
            try snapshot(host, to: URL(filePath: directory).appending(path: "approval-retry.png"))
        }
        companion.apply(snapshot: ["last_seq": 2, "pending_approvals": actions])
        companion.inputOpen = false
        let collapsed = NSHostingView(rootView: ApprovalDeck(companion: companion).frame(width: 288)
            .transaction { $0.disablesAnimations = true })
        window.contentView = collapsed
        collapsed.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(150))
        collapsed.layoutSubtreeIfNeeded()
        #expect(collapsed.fittingSize.height <= 300)

        companion.apply(snapshot: ["last_seq": 3, "pending_approvals": [actions[0]]])
        let deck = NSHostingView(rootView: ApprovalDeck(companion: companion).frame(width: 288)
            .transaction { $0.disablesAnimations = true })
        window.contentView = deck
        deck.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(150))
        deck.layoutSubtreeIfNeeded()
        #expect(deck.fittingSize.height >= 240)
        #expect(deck.fittingSize.height <= 430)
    }

    private func snapshot(_ view: NSView, to url: URL) throws {
        let bitmap = try #require(view.bitmapImageRepForCachingDisplay(in: view.bounds))
        view.cacheDisplay(in: view.bounds, to: bitmap)
        let data = try #require(bitmap.representation(using: .png, properties: [:]))
        try data.write(to: url)
    }
}
