import Foundation
import Testing
@testable import BudCompanion

@Suite struct BundledBrowseTests {
    private func folders() throws -> (contents: URL, home: URL) {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("bud-browse-\(UUID().uuidString)")
        let contents = root.appendingPathComponent("Contents")
        let helpers = contents.appendingPathComponent("Helpers")
        let resources = contents.appendingPathComponent("Resources")
        for folder in [helpers, resources] {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        }
        let program = helpers.appendingPathComponent("browse")
        try "#!/bin/sh\n".write(to: program, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: program.path)
        try "MIT".write(to: resources.appendingPathComponent("browse-LICENSE"), atomically: true, encoding: .utf8)
        return (contents, root.appendingPathComponent("home"))
    }

    @Test func copiesBrowseOnceAndLeavesAnUpdatedCopyAlone() throws {
        let (contents, home) = try folders()
        let target = home.appendingPathComponent(".bud/bin/browse")

        #expect(BundledBrowse.install(from: contents, home: home))
        let mode = try FileManager.default.attributesOfItem(atPath: target.path)[.posixPermissions]
        #expect(mode as? Int == 0o755)
        #expect(FileManager.default.fileExists(atPath: home.appendingPathComponent(".bud/bin/browse-LICENSE").path))

        try "updated".write(to: target, atomically: true, encoding: .utf8)
        #expect(!BundledBrowse.install(from: contents, home: home))
        #expect(try String(contentsOf: target, encoding: .utf8) == "updated")
    }

    @Test func doesNothingWithoutABundledCopy() throws {
        let (_, home) = try folders()
        #expect(!BundledBrowse.install(from: home.appendingPathComponent("nothing"), home: home))
    }
}
