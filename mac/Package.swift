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
        // On-device speech recognition: Parakeet TDT v3 converted to Core ML, with CTC
        // vocabulary boosting for names. Apache 2.0; models download on first setup.
        .package(url: "https://github.com/FluidInference/FluidAudio.git", from: "0.18.3"),
        .package(url: "https://github.com/mgriebling/SwiftMath.git", exact: "1.7.3"),
    ],
    targets: [
        .executableTarget(
            name: "BudCompanion",
            dependencies: [
                .product(name: "Markdown", package: "swift-markdown"),
                .product(name: "FluidAudio", package: "FluidAudio"),
                .product(name: "SwiftMath", package: "SwiftMath"),
            ],
            path: "Sources/BudCompanion",
            resources: [
                .process("Resources"),
            ]
        ),
        .testTarget(name: "BudCompanionTests", dependencies: ["BudCompanion"]),
    ]
)
