import AppKit
import SwiftUI

struct CapabilitySetup: Identifiable, Equatable {
    let id: String
    let label: String
    var status: String
    let kind: String
    var url: URL?
    let steps: [String]
    var reason: String?
    let setupID: String?

    init?(_ raw: [String: Any]) {
        guard let id = raw["capability"] as? String, !id.isEmpty,
              id.utf8.allSatisfy({ (97...122).contains($0) || (48...57).contains($0) || $0 == 95 })
        else { return nil }
        self.id = id
        label = raw["label"] as? String ?? id
        status = raw["status"] as? String ?? "waiting"
        kind = raw["kind"] as? String ?? "url"
        url = Self.signInURL(raw["url"] as? String)
        steps = raw["steps"] as? [String] ?? []
        reason = raw["reason"] as? String
        setupID = raw["setup_id"] as? String
    }

    static func signInURL(_ text: String?) -> URL? {
        guard let text, let url = URL(string: text),
              url.scheme == "https", url.host != nil, url.user == nil, url.password == nil
        else { return nil }
        return url
    }
}

@MainActor
final class SetupSignInRecovery: ObservableObject {
    @Published private(set) var pending = false
    @Published private(set) var expired = false
    @Published private(set) var problem: String?

    func needsNewLink(for setup: CapabilitySetup) -> Bool {
        expired || setup.status == "expired" || setup.reason?.localizedCaseInsensitiveContains("expired") == true
    }

    func openLink(
        for setup: CapabilitySetup,
        using companion: Companion,
        openBrowser: @escaping @MainActor (URL) -> Bool = { NSWorkspace.shared.open($0) }
    ) async {
        guard !pending, !companion.updatingSetups.contains(setup.id) else { return }
        let attempt = companion.connectionMachine.attempt
        func currentSetup() -> CapabilitySetup? {
            guard attempt == companion.connectionMachine.attempt else { return nil }
            return companion.setups.first { $0.id == setup.id && $0.setupID == setup.setupID }
        }
        guard let current = currentSetup() else { return }
        pending = true
        problem = nil
        defer { pending = false }
        do {
            let response = try await companion.sendOnboardingRequest(
                "POST", "connections/\(setup.id)/connect",
                json: ["resume": true, "refresh": needsNewLink(for: current)])
            guard let latest = currentSetup() else { return }
            if response["expired"] as? Bool == true {
                expired = true
                return
            }
            guard let url = CapabilitySetup.signInURL(response["url"] as? String) else {
                problem = "The sign-in link is unavailable. Try again."
                return
            }
            expired = false
            var updated: [String: Any] = [
                "capability": latest.id, "label": latest.label, "kind": latest.kind,
                "status": "waiting", "url": url.absoluteString, "steps": latest.steps,
            ]
            updated["setup_id"] = latest.setupID
            companion.receiveSetup(updated, replacing: true)
            companion.connectSetup(latest, openBrowser: openBrowser)
        } catch {
            guard currentSetup() != nil else { return }
            problem = "Couldn’t get the sign-in link. Try again."
            companion.refreshSetupStatus()
        }
    }

    func check(using companion: Companion) {
        problem = nil
        companion.refreshSetupStatus()
    }
}

struct SetupDeck: View {
    let companion: Companion
    @StateObject private var layout = ApprovalLayout()
    @Environment(\.replyType) private var replyType

    private var inWindow: Bool {
        !replyType.compact
    }

    private var limit: CGFloat {
        if inWindow { return 360 }
        if !companion.approvals.isEmpty { return 100 }
        return companion.inputOpen ? 140 : 190
    }

    var body: some View {
        if inWindow && companion.setups.count <= 2 {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(companion.setups) { setup in
                    SetupCard(companion: companion, setup: setup)
                        .id(setup.setupID)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: inWindow ? 10 : 8) {
                    ForEach(companion.setups) { setup in
                        SetupCard(companion: companion, setup: setup)
                            .id(setup.setupID)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { layout.height = $0 }
            }
            .frame(height: min(layout.height, limit))
            .scrollBounceBehavior(.basedOnSize)
        }
    }
}

struct SetupCard: View {
    let companion: Companion
    let setup: CapabilitySetup
    @Environment(\.replyType) private var replyType
    @StateObject private var recovery = SetupSignInRecovery()

    private var busy: Bool {
        recovery.pending || companion.updatingSetups.contains(setup.id) || companion.link != .connected
    }

    private var browserOpened: Bool {
        setup.kind == "url" && companion.openedSetups.contains(setup.id)
    }

    private var inWindow: Bool {
        !replyType.compact
    }

    private var needsNewLink: Bool { setup.kind == "url" && recovery.needsNewLink(for: setup) }

    private var instructions: String? {
        if needsNewLink { return nil }
        if browserOpened { return "Finish signing in in your browser." }
        if !setup.steps.isEmpty {
            return setup.steps.enumerated().map { "\($0.offset + 1). \($0.element)" }
                .joined(separator: "\n")
        }
        return inWindow ? "Sign in to \(setup.label) so bud can continue." : nil
    }

    private var problem: String? {
        recovery.problem ?? companion.setupProblems[setup.id] ?? (browserOpened ? nil : setup.reason)
    }

    var body: some View {
        if inWindow {
            windowCard
        } else {
            compactCard
        }
    }

    private var windowCard: some View {
        let shape = RoundedRectangle(cornerRadius: Paper.radiusLG, style: .continuous)
        return VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Text("Connect \(setup.label)")
                        .font(Paper.Typography.h3)
                        .tracking(Paper.Typography.h3Tracking)
                        .foregroundStyle(Paper.inkPrimary)
                        .lineLimit(1)
                    Spacer(minLength: 0)
                    signInStatus
                }
                if let instructions {
                    Text(instructions)
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let problem {
                    Text(problem)
                        .font(Paper.Typography.caption)
                        .foregroundStyle(Paper.errorInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 12)

            Rectangle().fill(Paper.hairline).frame(height: 0.5)

            actionRow
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .surface(
            shape,
            lift: 1,
            fill: Paper.elevated,
            border: Paper.borderSubtle,
            borderWidth: 0.5
        )
    }

    /// Compact inline layout kept for the floating pet bubble.
    private var compactCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            if browserOpened || needsNewLink {
                HStack(spacing: 8) {
                    compactTitle
                    Spacer(minLength: 0)
                    signInStatus
                }
            } else {
                HStack(spacing: 8) {
                    compactTitle
                    Spacer(minLength: 4)
                    cancelButton
                    signInActions
                }
            }
            if let instructions {
                Text(instructions)
                    .font(Paper.Typography.callout)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let problem {
                Text(problem)
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.errorInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if browserOpened || needsNewLink { actionRow }
        }
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var compactTitle: some View {
        Text(setup.label)
            .font(Paper.Typography.label)
            .lineLimit(1)
    }

    private var actionRow: some View {
        HStack(spacing: 8) {
            cancelButton
            Spacer(minLength: 8)
            signInActions
        }
    }

    private var cancelButton: some View {
        Button("Cancel") { companion.cancelSetup(setup) }
            .buttonStyle(CardButton(kind: .quiet))
            .accessibilityLabel("Cancel \(setup.label) setup")
            .disabled(busy)
    }

    @ViewBuilder private var signInStatus: some View {
        if needsNewLink {
            Text("Link expired")
                .font(Paper.Typography.caption)
                .foregroundStyle(Paper.inkSecondary)
        } else if browserOpened {
            Text("Waiting…")
                .font(Paper.Typography.caption)
                .foregroundStyle(Paper.inkSecondary)
            ProgressView().controlSize(.mini)
                .accessibilityHidden(true)
        }
    }

    @ViewBuilder private var signInActions: some View {
        if browserOpened || needsNewLink {
            Button("Check sign-in") { recovery.check(using: companion) }
                .buttonStyle(CardButton(kind: .quiet))
                .disabled(busy)
            Button(needsNewLink ? "Get a new link" : "Reopen") {
                Task { await recovery.openLink(for: setup, using: companion) }
            }
            .buttonStyle(CardButton(kind: .secondary))
            .disabled(busy)
            .opacity(busy ? 0.55 : 1)
        } else {
            if companion.updatingSetups.contains(setup.id) {
                ProgressView().controlSize(.small)
            }
            if setup.kind == "url" {
                Button { companion.connectSetup(setup) } label: {
                    Image(systemName: "arrow.up.right")
                }
                .buttonStyle(CardButton(kind: .primary))
                .accessibilityLabel("Connect \(setup.label)")
                .help("Open sign-in")
                .disabled(busy)
                .opacity(busy ? 0.55 : 1)
            }
        }
    }
}
