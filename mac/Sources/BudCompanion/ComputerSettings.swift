import AppKit
import SwiftUI

@MainActor @Observable
private final class ComputerSettingsState: ObservableObject {
    struct Computer: Identifiable, Equatable {
        let id: String
        let name: String
        var online = true
    }

    struct Pairing {
        let code: String
        let address: String
        let previous: Set<String>
    }

    var computers: [Computer] = []
    var macs: [Computer] = []
    var pairing: Pairing?
    var canPair = false
    var privateNetwork = false
    var busy = false
    var error: String?

    func refresh() async {
        if let response = try? await Companion.shared.sendOnboardingRequest("GET", "macs"),
           !Task.isCancelled {
            macs = (response["macs"] as? [[String: Any]] ?? []).compactMap { entry in
                guard let id = entry["id"] as? String, let name = entry["name"] as? String else { return nil }
                return Computer(id: id, name: name, online: entry["online"] as? Bool == true)
            }
        }
        do {
            let response = try await Companion.shared.sendOnboardingRequest("GET", "browser/computers")
            guard !Task.isCancelled else { return }
            canPair = response["available"] as? Bool == true
            privateNetwork = response["private_network"] as? Bool == true
            computers = (response["browsers"] as? [[String: String]] ?? []).compactMap { entry in
                guard let id = entry["id"], let name = entry["name"] else { return nil }
                return Computer(id: id, name: name)
            }
            if let pairing, !Set(computers.map(\.id)).isSubset(of: pairing.previous) {
                self.pairing = nil
            }
        } catch {
            guard !Task.isCancelled else { return }
            canPair = false
        }
    }

    func changeMac(_ id: String? = nil) async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            if let id {
                try await MacActionWorker.shared.revoke(id)
            } else {
                try await MacActionWorker.shared.pair()
            }
            await refresh()
        } catch {
            self.error = id == nil ? "Couldn’t pair this Mac." : "Couldn’t remove this Mac."
        }
    }

    func add(remote: Bool) async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let payload = try await Companion.shared.sendOnboardingRequest(
                "POST", "browser/computers/pair", json: ["browser": remote ? "remote" : "local"])
            guard let code = payload["code"] as? String, let address = payload["address"] as? String
            else { throw ConnectionError.notBudServer }
            pairing = Pairing(code: code, address: address, previous: Set(computers.map(\.id)))
        } catch ConnectionError.http(_, let detail) {
            error = detail
        } catch {
            self.error = "Couldn’t add a computer. Try again."
        }
    }
}

struct ComputerSettings: View {
    @StateObject private var state = ComputerSettingsState()
    @Bindable var companion = Companion.shared
    @Bindable var macWorker = MacActionWorker.shared

    private var budIsRemote: Bool { URL(string: BudSettings.load().url)?.scheme == "https" }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if budIsRemote {
                ForEach(state.macs) { mac in
                    HStack {
                        Text(mac.name)
                        Spacer()
                        Text(mac.online ? "Available for Mac apps" : "Offline")
                            .foregroundStyle(Paper.inkSecondary)
                        Button("Remove") { Task { await state.changeMac(mac.id) } }
                            .disabled(state.busy)
                    }
                    Divider()
                }
            }
            if state.computers.isEmpty {
                Text("No connected browsers")
                    .foregroundStyle(Paper.inkSecondary)
            }
            ForEach(state.computers) { computer in
                HStack {
                    Text(computer.name)
                    Spacer()
                    Text("Connected").foregroundStyle(Paper.inkSecondary)
                }
                Divider()
            }
            if let pairing = state.pairing {
                pairingDetails(pairing)
            } else {
                HStack {
                    Spacer()
                    if state.busy { ProgressView().controlSize(.small) }
                    Menu("Add a computer…") {
                        if budIsRemote && macWorker.localID == nil {
                            Button("Pair this Mac") { Task { await state.changeMac() } }
                        }
                        Button("Chrome on this Mac") { Task { await state.add(remote: budIsRemote) } }
                            .disabled(!state.canPair)
                        Button("Daytona") { Task { await state.add(remote: true) } }
                            .disabled(!state.canPair)
                    }
                    .fixedSize()
                    .disabled(state.busy || companion.link != .connected
                              || (!state.canPair && (!budIsRemote || macWorker.localID != nil)))
                }
            }
            if let error = state.error {
                Text(error).font(Paper.Typography.callout).foregroundStyle(Paper.errorInk)
            } else if companion.link != .connected {
                Button("Try Again", action: companion.start)
            } else if state.pairing == nil && !state.privateNetwork {
                Text("Use a private network to add computers that aren’t running the Bud server.")
                    .font(Paper.Typography.callout)
                    .foregroundStyle(Paper.inkSecondary)
            }
        }
        .font(Paper.Typography.body)
        .task(id: companion.link == .connected) {
            while !Task.isCancelled {
                await state.refresh()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    private func pairingDetails(_ pairing: ComputerSettingsState.Pairing) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Open the Browse extension on that computer. Choose **Pair with a code**.")
                .font(Paper.Typography.callout)
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    Text("Relay address")
                    Text(pairing.address).textSelection(.enabled)
                    Button("Copy") { copy(pairing.address) }
                        .accessibilityLabel("Copy relay address")
                }
                GridRow {
                    Text("Pairing code")
                    Text(pairing.code).textSelection(.enabled)
                    Button("Copy") { copy(pairing.code) }
                        .accessibilityLabel("Copy pairing code")
                }
            }
            HStack {
                SwiftUI.Link("Get Browse", destination: URL(
                    string: "https://chromewebstore.google.com/detail/opencode-browse/mfnicocicmmlkpjnaffgihfjhdgjkdjg")!)
                SwiftUI.Link("Open Daytona", destination: URL(string: "https://app.daytona.io")!)
                Spacer()
                Button("Done") { state.pairing = nil }
            }
        }
    }

    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
