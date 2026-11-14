// swift-tools-version: 6.0
// The bud desktop companion: a floating pet that talks to a running bud server.
import PackageDescription

let package = Package(
    name: "BudCompanion",
    platforms: [.macOS(.v15)],
    dependencies: [
        // Apple's CommonMark + GitHub-flavored markdown parser (cmark-gfm). A parser only, with
        // no SwiftUI, so it builds without Xcode; the views that draw replies are ours.
        .package(url: "https://github.com/swiftlang/swift-markdown.git", from: "0.9.0"),
    ],
    targets: [
        .executableTarget(
            name: "BudCompanion",
            dependencies: [.product(name: "Markdown", package: "swift-markdown")],
            path: "Sources/BudCompanion"
        )
    ]
)
