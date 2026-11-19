import Foundation

/// The app ships OpenCode Browse and puts a copy where Bud's server looks for it, ~/.bud/bin.
/// A copy already there is left alone: Browse updates itself in place.
enum BundledBrowse {
    /// Copies the bundled program and its license when ~/.bud/bin has no browse yet.
    /// Returns whether a copy was made.
    @discardableResult
    static func install(
        from resources: URL? = Bundle.main.resourceURL,
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> Bool {
        let files = FileManager.default
        guard let source = resources?.appendingPathComponent("browse"),
              files.fileExists(atPath: source.appendingPathComponent("browse").path)
        else { return false }
        let bin = home.appendingPathComponent(".bud/bin")
        let target = bin.appendingPathComponent("browse")
        guard !files.fileExists(atPath: target.path) else { return false }
        do {
            try files.createDirectory(
                at: bin, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try files.copyItem(at: source.appendingPathComponent("browse"), to: target)
            try files.setAttributes([.posixPermissions: 0o755], ofItemAtPath: target.path)
            let license = bin.appendingPathComponent("browse-LICENSE")
            if !files.fileExists(atPath: license.path) {
                try? files.copyItem(at: source.appendingPathComponent("LICENSE"), to: license)
            }
            return true
        } catch {
            try? files.removeItem(at: target)  // never leave half a program behind
            return false
        }
    }
}
