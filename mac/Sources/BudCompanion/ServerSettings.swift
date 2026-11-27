import SwiftUI

@MainActor @Observable
final class ServerSettingsDraft {
    enum Field: Hashable { case address, token }

    struct FieldFocus: Equatable {
        let field: Field
        private let id = UUID()
    }

    var address = "" { didSet { addressError = nil } }
    var token = "" { didSet { tokenError = nil } }
    private(set) var addressError: String?
    private(set) var tokenError: String?
    private(set) var generalError: String?
    private(set) var fieldFocus: FieldFocus?
    var checking: Bool { checkID != nil }

    private var checkID: UUID?
    @ObservationIgnored private var checkTask: Task<Void, Never>?
    private let verify: @MainActor (BudConnection) async throws -> Void
    private let save: @MainActor (BudConnection) throws -> Void

    init(
        verify: @escaping @MainActor (BudConnection) async throws -> Void = RemoteBudServer.verify,
        save: @escaping @MainActor (BudConnection) throws -> Void = { connection in
            try RemoteBudServer.save(token: connection.token, for: connection.base)
            BudSettings.useRemote(connection.base)
        }
    ) {
        self.verify = verify
        self.save = save
    }

    @discardableResult
    func connect(connected: @escaping @MainActor () -> Void) -> Task<Void, Never>? {
        guard !checking else { return nil }
        addressError = nil
        tokenError = nil
        generalError = nil
        guard let origin = RemoteBudServer.origin(address) else {
            showFieldError("Enter the server’s HTTPS address.", for: .address)
            return nil
        }
        let credential = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard BudConnection.valid(credential) else {
            showFieldError("Enter the access token from your Bud server.", for: .token)
            return nil
        }
        let connection = BudConnection(base: origin, token: credential)
        let id = UUID()
        checkID = id
        checkTask = Task { [weak self, verify, save] in
            guard !Task.isCancelled else { return }
            let result: Result<Void, Error>
            do {
                try await verify(connection)
                result = .success(())
            } catch {
                result = .failure(error)
            }
            guard let self, checkID == id, !Task.isCancelled else { return }
            checkID = nil
            checkTask = nil

            if case .failure(let failure) = result {
                showVerificationError(failure)
                return
            }
            do {
                try save(connection)
                connected()
            } catch let failure as ConnectionError {
                generalError = failure.description
            } catch {
                generalError = "Couldn’t save this Bud server."
            }
        }
        return checkTask
    }

    func cancelCheck() {
        checkID = nil
        checkTask?.cancel()
        checkTask = nil
    }

    private func showFieldError(_ message: String, for field: Field) {
        switch field {
        case .address: addressError = message
        case .token: tokenError = message
        }
        fieldFocus = FieldFocus(field: field)
    }

    private func showVerificationError(_ error: Error) {
        switch error as? ConnectionError {
        case .http(let code, _) where code == 401 || code == 403:
            showFieldError("The server didn’t accept this access token.", for: .token)
        case .http(409, let message):
            generalError = message
        case .unreachable:
            generalError = "Couldn’t reach the server. Check the address and try again."
        case .badURL, .notBudServer:
            showFieldError(String(describing: error), for: .address)
        case .keychainUnavailable:
            generalError = String(describing: error)
        default:
            generalError = "Couldn’t verify this Bud server."
        }
    }
}

struct ServerFields: View {
    @Bindable var draft: ServerSettingsDraft
    var lockAddress = false
    @FocusState private var focusedField: ServerSettingsDraft.Field?

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 10) {
            GridRow(alignment: .firstTextBaseline) {
                Text("Server address")
                VStack(alignment: .leading, spacing: 4) {
                    TextField("https://bud.example.com", text: $draft.address)
                        .accessibilityLabel("Server address")
                        .autocorrectionDisabled()
                        .focused($focusedField, equals: .address)
                        .disabled(lockAddress)
                    fieldError(draft.addressError)
                }
            }
            GridRow(alignment: .firstTextBaseline) {
                Text("Access token")
                VStack(alignment: .leading, spacing: 4) {
                    SecureField("", text: $draft.token)
                        .accessibilityLabel("Access token")
                        .focused($focusedField, equals: .token)
                    fieldError(draft.tokenError)
                }
            }
        }
        .disabled(draft.checking)
        .onChange(of: draft.fieldFocus) { _, request in
            focusedField = request?.field
        }
    }

    @ViewBuilder
    private func fieldError(_ message: String?) -> some View {
        if let message {
            Text(message)
                .font(Paper.Typography.callout)
                .foregroundStyle(Paper.errorInk)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct ServerSettings: View {
    @Bindable var draft: ServerSettingsDraft
    let cancel: () -> Void
    let connected: @MainActor () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Connect to a server")
                .font(Paper.Typography.h2)
            ServerFields(draft: draft)
            HStack(spacing: 10) {
                if draft.checking {
                    ProgressView().controlSize(.small)
                    Text("Checking connection…")
                        .foregroundStyle(Paper.inkSecondary)
                } else if let error = draft.generalError {
                    Text(error)
                        .font(Paper.Typography.callout)
                        .foregroundStyle(Paper.errorInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Button("Cancel", action: dismiss)
                    .keyboardShortcut(.cancelAction)
                Button("Connect") { draft.connect(connected: connected) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(draft.checking || draft.address.isEmpty || draft.token.isEmpty)
            }
        }
        .padding(24)
        .frame(width: 450)
        .background(Paper.canvas)
        .onExitCommand(perform: dismiss)
        .onDisappear(perform: draft.cancelCheck)
    }

    private func dismiss() {
        draft.cancelCheck()
        cancel()
    }
}
