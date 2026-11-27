import SwiftUI

struct CapabilitySetup: Identifiable, Equatable {
    let id: String
    let label: String
    var status: String
    let kind: String
    var url: URL?
    let steps: [String]
    var reason: String?

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
    }

    static func signInURL(_ text: String?) -> URL? {
        guard let text, let url = URL(string: text),
              url.scheme == "https", url.host != nil, url.user == nil, url.password == nil
        else { return nil }
        return url
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
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: inWindow ? 10 : 8) {
                    ForEach(companion.setups) { setup in
                        SetupCard(companion: companion, setup: setup)
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

    private var busy: Bool {
        companion.updatingSetups.contains(setup.id) || companion.link != .connected
    }

    private var inWindow: Bool {
        !replyType.compact
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
                Text("Connect \(setup.label)")
                    .font(Paper.Typography.h3)
                    .tracking(Paper.Typography.h3Tracking)
                    .foregroundStyle(Paper.inkPrimary)
                    .lineLimit(1)
                if !setup.steps.isEmpty {
                    Text(setup.steps.enumerated().map { "\($0.offset + 1). \($0.element)" }
                        .joined(separator: "\n"))
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("Sign in to \(setup.label) so bud can continue.")
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.inkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let problem = companion.setupProblems[setup.id] ?? setup.reason {
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

            HStack(spacing: 8) {
                Button("Cancel") { companion.cancelSetup(setup) }
                    .buttonStyle(CardButton(kind: .quiet))
                    .accessibilityLabel("Cancel \(setup.label) setup")
                    .disabled(busy)
                Spacer(minLength: 8)
                if companion.updatingSetups.contains(setup.id) {
                    ProgressView().controlSize(.small)
                }
                if setup.kind == "url" {
                    Button("Connect") { companion.connectSetup(setup) }
                        .buttonStyle(CardButton(kind: .primary))
                        .accessibilityLabel("Connect \(setup.label)")
                        .disabled(busy)
                        .opacity(busy ? 0.55 : 1)
                }
            }
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
            HStack(spacing: 8) {
                Text(setup.label)
                    .font(Paper.Typography.label)
                    .lineLimit(1)
                Spacer(minLength: 4)
                Button("Cancel") { companion.cancelSetup(setup) }
                    .buttonStyle(CardButton(kind: .quiet))
                    .accessibilityLabel("Cancel \(setup.label) setup")
                    .disabled(busy)
                if setup.kind == "url" {
                    Button("Connect") { companion.connectSetup(setup) }
                        .buttonStyle(CardButton(kind: .primary))
                        .accessibilityLabel("Connect \(setup.label)")
                        .disabled(busy)
                        .opacity(busy ? 0.55 : 1)
                }
                if companion.updatingSetups.contains(setup.id) {
                    ProgressView().controlSize(.small)
                }
            }
            if !setup.steps.isEmpty {
                Text(setup.steps.enumerated().map { "\($0.offset + 1). \($0.element)" }
                    .joined(separator: "\n"))
                    .font(Paper.Typography.callout)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let problem = companion.setupProblems[setup.id] ?? setup.reason {
                Text(problem)
                    .font(Paper.Typography.caption)
                    .foregroundStyle(Paper.errorInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
