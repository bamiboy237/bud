import Foundation
import Security

enum RemoteBudServer {
    static func origin(_ address: String) -> URL? {
        guard var parts = URLComponents(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme?.lowercased() == "https",
              let host = parts.host?.lowercased(), !host.isEmpty,
              parts.port.map({ (1...65535).contains($0) }) ?? true,
              let url = parts.url, BudConnection.safeOrigin(url)
        else { return nil }
        parts.scheme = "https"
        parts.host = host
        if parts.port == 443 { parts.port = nil }
        parts.path = ""
        return parts.url
    }

    static func verify(_ connection: BudConnection) async throws {
        let runtime = try await connection.send("GET", "runtime")
        guard let scope = runtime["scope"] as? String, !scope.isEmpty,
              runtime["version"] is String, runtime["needs_onboarding"] is Bool
        else { throw ConnectionError.notBudServer }
        if runtime["mode"] as? String == "receiving" {
            throw ConnectionError.http(409, "This server is waiting for Move Bud.")
        }
    }

    private static func query(_ origin: URL, purpose: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "\(Bundle.main.bundleIdentifier ?? "app.bud.companion").\(purpose)",
            kSecAttrAccount as String: origin.absoluteString,
        ]
    }

    static func save(token: String, for origin: URL, purpose: String = "server") throws {
        var attributes = query(origin, purpose: purpose)
        let value = Data(token.utf8)
        attributes[kSecValueData as String] = value
        var status = SecItemAdd(attributes as CFDictionary, nil)
        if status == errSecDuplicateItem {
            status = SecItemUpdate(query(origin, purpose: purpose) as CFDictionary, [
                kSecValueData as String: value
            ] as CFDictionary)
        }
        guard status == errSecSuccess else { throw ConnectionError.keychainUnavailable }
    }

    static func token(for origin: URL, purpose: String = "server") throws -> String {
        var attributes = query(origin, purpose: purpose)
        attributes[kSecReturnData as String] = true
        attributes[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(attributes as CFDictionary, &result)
        if status == errSecItemNotFound {
            throw ConnectionError.noToken("Enter the access token for this server in Settings.")
        }
        guard status == errSecSuccess, let data = result as? Data,
              let token = String(data: data, encoding: .utf8), BudConnection.valid(token)
        else { throw ConnectionError.keychainUnavailable }
        return token
    }

    static func removeToken(for origin: URL, purpose: String) {
        SecItemDelete(query(origin, purpose: purpose) as CFDictionary)
    }
}
