import AppKit
import SwiftUI

@MainActor @Observable
final class ComputerSettingsState: ObservableObject {
    enum BrowserLoad: Equatable { case checking, loaded, failed }

    struct Computer: Identifiable, Equatable {
        let id: String
        let name: String
        var online = true
    }

    struct Pairing {
        enum Destination: String {
            case chrome = "Chrome"
            case daytona = "Daytona"
        }

        let destination: Destination
        let code: String
        let address: String
        private(set) var connectedBrowser: Computer?

        var title: String {
            if let connectedBrowser { return "Connected to \(connectedBrowser.name)" }
            return "Pair \(destination.rawValue)"
        }

        mutating func confirm(_ receipt: [String: String]?) {
            guard connectedBrowser == nil, let receipt, receipt["code"] == code,
                  let id = receipt["id"], !id.isEmpty,
                  let name = receipt["name"], !name.isEmpty else { return }
            connectedBrowser = Computer(id: id, name: name)
        }
    }

    private(set) var computers: [Computer] = []
    private(set) var browserLoad = BrowserLoad.checking
    var macs: [Computer] = []
    var pairing: Pairing?
    var canPair = false
    var privateNetwork = false
    var busy = false
    var error: String?

    var browserStatus: String? {
        switch browserLoad {
        case .checking: "Checking browsers…"
        case .loaded: computers.isEmpty ? "No connected browsers" : nil
        case .failed: "Couldn't load browsers"
        }
    }

    func refresh() async {
        await refreshMacs()
        guard !Task.isCancelled else { return }
        do {
            let response = try await Companion.shared.sendOnboardingRequest("GET", "browser/computers")
            guard !Task.isCancelled else { return }
            updateBrowsers(response)
        } catch {
            guard !Task.isCancelled else { return }
            browserLoadFailed()
        }
    }

    private func refreshMacs() async {
        guard let response = try? await Companion.shared.sendOnboardingRequest("GET", "macs"),
              !Task.isCancelled else { return }
        macs = (response["macs"] as? [[String: Any]] ?? []).compactMap { entry in
            guard let id = entry["id"] as? String, let name = entry["name"] as? String else { return nil }
            return Computer(id: id, name: name, online: entry["online"] as? Bool == true)
        }
    }

    func updateBrowsers(_ response: [String: Any]) {
        guard let browsers = response["browsers"] as? [[String: String]] else {
            browserLoadFailed()
            return
        }
        canPair = response["available"] as? Bool == true
        privateNetwork = response["private_network"] as? Bool == true
        computers = browsers.compactMap { entry in
            guard let id = entry["id"], let name = entry["name"] else { return nil }
            return Computer(id: id, name: name)
        }
        pairing?.confirm(response["pairing"] as? [String: String])
        browserLoad = .loaded
    }

    private func browserLoadFailed() {
        browserLoad = .failed
        canPair = false
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
            await refreshMacs()
        } catch {
            self.error = id == nil ? "Couldn’t pair this Mac." : "Couldn’t remove this Mac."
        }
    }

    func add(_ destination: Pairing.Destination, budIsRemote: Bool) async {
        guard !busy else { return }
        busy = true
        error = nil
        defer { busy = false }
        do {
            let remote = budIsRemote || destination == .daytona
            let payload = try await Companion.shared.sendOnboardingRequest(
                "POST", "browser/computers/pair", json: ["browser": remote ? "remote" : "local"])
            guard let code = payload["code"] as? String, let address = payload["address"] as? String
            else { throw ConnectionError.notBudServer }
            pairing = Pairing(destination: destination, code: code, address: address)
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
            if let status = state.browserStatus {
                HStack(spacing: 8) {
                    if state.browserLoad == .checking { ProgressView().controlSize(.small) }
                    Text(status).foregroundStyle(Paper.inkSecondary)
                }
                .accessibilityElement(children: .combine)
            }
            ForEach(state.computers) { computer in
                HStack {
                    Text(computer.name)
                    Spacer()
                    Text(state.browserLoad == .failed ? "Last seen connected" : "Connected")
                        .foregroundStyle(Paper.inkSecondary)
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
                        Button("Chrome on this Mac") {
                            Task { await state.add(.chrome, budIsRemote: budIsRemote) }
                        }
                        .disabled(!state.canPair)
                        Button("Daytona") {
                            Task { await state.add(.daytona, budIsRemote: budIsRemote) }
                        }
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
            } else if state.browserLoad == .loaded && state.pairing == nil && !state.privateNetwork {
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
            Text(pairing.title)
                .font(Paper.Typography.h2)
                .accessibilityAddTraits(.isHeader)
            if pairing.connectedBrowser == nil {
                pairingInstructions(pairing)
            } else {
                Button("Done") { state.pairing = nil }
            }
        }
    }

    private func pairingInstructions(_ pairing: ComputerSettingsState.Pairing) -> some View {
        Group {
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
                if pairing.destination == .daytona {
                    SwiftUI.Link("Open Daytona", destination: URL(string: "https://app.daytona.io")!)
                }
                Spacer()
                Button("Get a new code") {
                    Task { await state.add(pairing.destination, budIsRemote: budIsRemote) }
                }
                .disabled(state.busy || !state.canPair || companion.link != .connected)
            }
        }
    }

    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
