import SwiftUI

@MainActor @Observable
final class ServerSettingsDraft {
    var address = ""
    var token = ""
    var checking = false
    var error: String?

    func connect() async -> Bool {
        guard let connection = await verify() else { return false }
        do {
            try save(connection)
            return true
        } catch let failure as ConnectionError {
            error = failure.description
        } catch {
            self.error = "Couldn’t save this Bud server."
        }
        return false
    }

    func save(_ connection: BudConnection) throws {
        try RemoteBudServer.save(token: connection.token, for: connection.base)
        BudSettings.useRemote(connection.base)
    }

    func verify() async -> BudConnection? {
        guard let origin = RemoteBudServer.origin(address) else {
            error = "Enter the server’s HTTPS address."
            return nil
        }
        let credential = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard BudConnection.valid(credential) else {
            error = "Enter the access token from your Bud server."
            return nil
        }
        let connection = BudConnection(base: origin, token: credential)
        checking = true
        error = nil
        defer { checking = false }
        do {
            try await RemoteBudServer.verify(connection)
            try Task.checkCancellation()
            return connection
        } catch ConnectionError.http(let code, _) where code == 401 || code == 403 {
            error = "The server didn’t accept this access token."
        } catch ConnectionError.http(409, let message) {
            error = message
        } catch ConnectionError.unreachable {
            error = "Couldn’t reach the server. Check the address and try again."
        } catch let failure as ConnectionError {
            switch failure {
            case .notBudServer, .keychainUnavailable: error = failure.description
            default: error = "Couldn’t verify this Bud server."
            }
        } catch {
            self.error = "Couldn’t verify this Bud server."
        }
        return nil
    }
}

struct ServerFields: View {
    @Bindable var draft: ServerSettingsDraft
    var lockAddress = false

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 10) {
            GridRow {
                Text("Server address")
                TextField("https://bud.example.com", text: $draft.address)
                    .accessibilityLabel("Server address")
                    .autocorrectionDisabled()
                    .disabled(lockAddress)
            }
            GridRow {
                Text("Access token")
                SecureField("", text: $draft.token)
                    .accessibilityLabel("Access token")
            }
        }
        .disabled(draft.checking)
    }
}

struct ServerSettings: View {
    @Bindable var draft: ServerSettingsDraft
    let cancel: () -> Void
    let connected: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Connect to a server")
                .font(Paper.Typography.h2)
            ServerFields(draft: draft)
            if let error = draft.error {
                Text(error)
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.errorInk)
            }
            HStack(spacing: 10) {
                if draft.checking {
                    ProgressView().controlSize(.small)
                    Text("Checking connection…")
                        .foregroundStyle(Paper.inkSecondary)
                }
                Spacer()
                Button("Cancel", action: cancel)
                    .keyboardShortcut(.cancelAction)
                    .disabled(draft.checking)
                Button("Connect") {
                    Task {
                        if await draft.connect() { connected() }
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(draft.checking || draft.address.isEmpty || draft.token.isEmpty)
            }
        }
        .padding(24)
        .frame(width: 450)
        .background(Paper.canvas)
    }
}
