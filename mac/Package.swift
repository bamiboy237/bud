// swift-tools-version: 6.0
// The bud desktop companion: a floating pet that talks to a running bud server.
import PackageDescription

let package = Package(
    name: "BudCompanion",
    platforms: [.macOS(.v15)],
    targets: [
        .executableTarget(name: "BudCompanion", path: "Sources/BudCompanion")
    ]
)
