import Foundation
import Testing
@testable import BudCompanion

@Suite struct BundledBrowseTests {
    private func folders() throws -> (resources: URL, home: URL) {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("bud-browse-\(UUID().uuidString)")
        let resources = root.appendingPathComponent("Resources")
        let browse = resources.appendingPathComponent("browse")
        try FileManager.default.createDirectory(at: browse, withIntermediateDirectories: true)
        try "#!/bin/sh\n".write(to: browse.appendingPathComponent("browse"), atomically: true, encoding: .utf8)
        try "MIT".write(to: browse.appendingPathComponent("LICENSE"), atomically: true, encoding: .utf8)
        return (resources, root.appendingPathComponent("home"))
    }

    @Test func copiesBrowseOnceAndLeavesAnUpdatedCopyAlone() throws {
        let (resources, home) = try folders()
        let target = home.appendingPathComponent(".bud/bin/browse")

        #expect(BundledBrowse.install(from: resources, home: home))
        let mode = try FileManager.default.attributesOfItem(atPath: target.path)[.posixPermissions]
        #expect(mode as? Int == 0o755)
        #expect(FileManager.default.fileExists(atPath: home.appendingPathComponent(".bud/bin/browse-LICENSE").path))

        try "updated".write(to: target, atomically: true, encoding: .utf8)
        #expect(!BundledBrowse.install(from: resources, home: home))
        #expect(try String(contentsOf: target, encoding: .utf8) == "updated")
    }

    @Test func doesNothingWithoutABundledCopy() throws {
        let (_, home) = try folders()
        #expect(!BundledBrowse.install(from: home.appendingPathComponent("nothing"), home: home))
    }
}
