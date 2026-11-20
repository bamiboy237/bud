import Foundation

/// The app ships OpenCode Browse and puts a copy where Bud's server looks for it, ~/.bud/bin.
/// A copy already there is left alone: Browse updates itself in place.
enum BundledBrowse {
    /// Copies the bundled program and its license when ~/.bud/bin has no browse yet.
    /// Returns whether a copy was made. The copy appears whole or not at all.
    @discardableResult
    static func install(
        from contents: URL? = Bundle.main.bundleURL.appendingPathComponent("Contents"),
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> Bool {
        let files = FileManager.default
        guard let contents else { return false }
        let program = contents.appendingPathComponent("Helpers/browse")
        guard files.isExecutableFile(atPath: program.path) else { return false }
        let bin = home.appendingPathComponent(".bud/bin")
        let target = bin.appendingPathComponent("browse")
        guard !files.fileExists(atPath: target.path) else { return false }
        let partial = bin.appendingPathComponent(".browse-\(UUID().uuidString)")
        do {
            try files.createDirectory(
                at: bin, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try files.copyItem(at: program, to: partial)
            try files.setAttributes([.posixPermissions: 0o755], ofItemAtPath: partial.path)
            // A rename is atomic: Bud's server never sees half a program.
            try files.moveItem(at: partial, to: target)
        } catch {
            try? files.removeItem(at: partial)
            return false
        }
        let license = contents.appendingPathComponent("Resources/browse-LICENSE")
        try? files.copyItem(at: license, to: bin.appendingPathComponent("browse-LICENSE"))
        return true
    }
}
