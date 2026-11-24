import Foundation

/// Non-secret runtime metadata used to render a diagnostic snapshot for clipboard export.
/// Never include `OWNER_TOKEN`, auth headers, API keys, or conversation message bodies here.
struct DiagnosticsSnapshot: Equatable, Sendable {
    var appVersion: String
    var appBuild: String
    var macOSVersion: String
    var connectionPhase: String
    var connectionAttempt: Int
    var backoffAttempt: Int
    var sessionID: String
    var serverURL: String
    var petStatus: String
    var running: Bool
    var pendingApprovalsCount: Int
    var pendingSetupsCount: Int
    var activeTasksCount: Int
    var stalledTasksCount: Int
    var decidingCount: Int
    var approvingBatch: Bool
    var voiceState: String
    var voiceKey: String
    var needsAccessibility: Bool
    var recentNotices: [String]
    var recentErrors: [String]
    var serverVersion: String? = nil
}

/// Formats and sanitizes diagnostic snapshots so credentials, tokens, URL query strings,
/// and auth headers can never leak onto the clipboard.
enum DiagnosticsFormatter {
    /// Formats a URL down to `scheme://host[:port]`, stripping userinfo, path, query, and fragment.
    static func sanitizedEndpoint(from rawURL: String) -> String {
        let trimmed = rawURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "(none)" }
        guard var components = URLComponents(string: trimmed),
              let scheme = components.scheme, !scheme.isEmpty,
              let host = components.host, !host.isEmpty
        else {
            return "(invalid URL)"
        }
        components.user = nil
        components.password = nil
        components.path = ""
        components.query = nil
        components.fragment = nil
        return components.string ?? "\(scheme)://\(host)"
    }

    /// Scrubs known secret values and common credential patterns (Bearer tokens, API keys,
    /// `OWNER_TOKEN=...`, and URL credentials/queries) from free-form error or notice text.
    static func redact(_ text: String, knownSecrets: [String] = []) -> String {
        var result = text
        for secret in knownSecrets where !secret.isEmpty {
            result = result.replacingOccurrences(of: secret, with: "[REDACTED]")
        }
        let patterns: [(pattern: String, replacement: String)] = [
            (#"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*"#, "Bearer [REDACTED]"),
            (#"(?i)\b(OWNER_TOKEN|X-API-KEY|API_KEY|ACCESS_TOKEN|AUTH_TOKEN|REFRESH_TOKEN|SECRET_KEY|CLIENT_SECRET|PASSWORD)\b\s*[:=]\s*[^\s,;]+"#, "$1=[REDACTED]"),
            (#"(?i)\b(Authorization|x-bud-scope)\b\s*:\s*[^\r\n]+"#, "$1: [REDACTED]"),
            (#"\b(?:sk|pk|ghp|gho|ghu|ghs|xox[baprs])[-_][A-Za-z0-9\-_]{10,}\b"#, "[REDACTED]"),
            (#"(https?://)[^/\s:@]+:[^/\s@]+@"#, "$1[REDACTED]@"),
            (#"(https?://[^\s?#]+)\?[^\s#]+"#, "$1?[REDACTED]"),
        ]
        for (pattern, replacement) in patterns {
            if let regex = try? NSRegularExpression(pattern: pattern) {
                let range = NSRange(result.startIndex..<result.endIndex, in: result)
                result = regex.stringByReplacingMatches(
                    in: result, options: [], range: range, withTemplate: replacement)
            }
        }
        return result
    }

    static func format(_ snapshot: DiagnosticsSnapshot, knownSecrets: [String] = []) -> String {
        let cleanEndpoint = sanitizedEndpoint(from: snapshot.serverURL)
        let session = snapshot.sessionID.isEmpty ? "(none)" : redact(snapshot.sessionID, knownSecrets: knownSecrets)
        var lines: [String] = [
            "Bud Companion Diagnostics",
            "-------------------------",
            "App: \(snapshot.appVersion) (\(snapshot.appBuild))",
            "macOS: \(snapshot.macOSVersion)",
            "Connection: \(snapshot.connectionPhase) (attempt #\(snapshot.connectionAttempt), backoff #\(snapshot.backoffAttempt))",
            "Server: \(cleanEndpoint)",
        ]
        if let serverVersion = snapshot.serverVersion, !serverVersion.isEmpty {
            lines.append("Server version: \(redact(serverVersion, knownSecrets: knownSecrets))")
        }
        lines += [
            "Session: \(session)",
            "Pet: \(snapshot.petStatus) (running: \(snapshot.running ? "yes" : "no"))",
            "Pending: approvals=\(snapshot.pendingApprovalsCount), setups=\(snapshot.pendingSetupsCount), tasks=\(snapshot.activeTasksCount) (stalled=\(snapshot.stalledTasksCount))",
            "Deck: deciding=\(snapshot.decidingCount), batch=\(snapshot.approvingBatch ? "yes" : "no")",
            "Voice: \(redact(snapshot.voiceState, knownSecrets: knownSecrets)) (key: \(snapshot.voiceKey), accessibility_needed: \(snapshot.needsAccessibility ? "yes" : "no"))",
        ]

        let notices = snapshot.recentNotices
            .map { redact($0, knownSecrets: knownSecrets).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        if notices.isEmpty {
            lines.append("Recent notices: none")
        } else {
            lines.append("Recent notices:")
            for notice in notices {
                lines.append("  - \(notice)")
            }
        }

        let errors = snapshot.recentErrors
            .map { redact($0, knownSecrets: knownSecrets).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        if errors.isEmpty {
            lines.append("Recent errors: none")
        } else {
            lines.append("Recent errors:")
            for error in errors {
                lines.append("  - \(error)")
            }
        }

        return lines.joined(separator: "\n")
    }
}
