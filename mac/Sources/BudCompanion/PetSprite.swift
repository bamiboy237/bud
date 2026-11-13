import AppKit
import SwiftUI

/// One animated row of a Codex-format pet sheet: 8 columns by 9 rows of 192x208 px cells.
struct PetRow: Sendable {
    let index: Int
    let frames: Int
    /// Every frame but the last, then the last: the hold at the end gives each loop a beat.
    let frameMs: Int
    let lastMs: Int

    var loopMs: Int { frameMs * (frames - 1) + lastMs }

    /// The frame to show `ms` into the state.
    func frame(at ms: Int) -> Int {
        let t = ms % loopMs
        return min(t / frameMs, frames - 1)
    }

    // Row order and timings from the Codex pet contract (openai/codex#20863).
    static let idle = PetRow(index: 0, frames: 6, frameMs: 140, lastMs: 320)
    static let waving = PetRow(index: 3, frames: 4, frameMs: 140, lastMs: 280)
    static let failed = PetRow(index: 5, frames: 8, frameMs: 140, lastMs: 240)
    static let waiting = PetRow(index: 6, frames: 6, frameMs: 150, lastMs: 260)
    static let running = PetRow(index: 7, frames: 6, frameMs: 120, lastMs: 220)
    static let review = PetRow(index: 8, frames: 6, frameMs: 150, lastMs: 280)

    static func of(_ status: PetStatus) -> PetRow {
        switch status {
        case .idle: .idle
        case .working: .running
        case .needsYou: .waiting
        case .done: .review
        case .failed: .failed
        }
    }
}

/// A pet's frames, cut from its sheet once. Pets are private art, so none ships in the
/// repository: the build copies local ones into the app, and users can drop one into
/// ~/Library/Application Support/bud/pets/<id>/. Without one, the companion shows a paw.
@MainActor
final class PetSprite {
    static let cell = CGSize(width: 192, height: 208)
    private static var cache: [String: PetSprite?] = [:]

    private let sheet: CGImage
    private var frames: [Int: NSImage] = [:]

    private init(sheet: CGImage) { self.sheet = sheet }

    static func named(_ id: String) -> PetSprite? {
        if let hit = cache[id] { return hit }
        let sprite = folders(id).lazy.compactMap(load).first
        cache[id] = sprite
        return sprite
    }

    func image(row: PetRow, frame: Int) -> NSImage? {
        let key = row.index * 8 + frame
        if let hit = frames[key] { return hit }
        let rect = CGRect(
            x: CGFloat(frame) * Self.cell.width, y: CGFloat(row.index) * Self.cell.height,
            width: Self.cell.width, height: Self.cell.height)
        guard let cut = sheet.cropping(to: rect) else { return nil }
        // Points are half the pixels, so the frame stays sharp on a 2x display.
        let image = NSImage(cgImage: cut, size: NSSize(width: rect.width / 2, height: rect.height / 2))
        frames[key] = image
        return image
    }

    private static func folders(_ id: String) -> [URL] {
        var found: [URL] = []
        if let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first {
            found.append(support.appending(path: "bud/pets/\(id)"))
        }
        if let resources = Bundle.main.resourceURL {
            found.append(resources.appending(path: "pets/\(id)"))
        }
        return found
    }

    private static func load(_ folder: URL) -> PetSprite? {
        guard
            let data = try? Data(contentsOf: folder.appending(path: "pet.json")),
            let manifest = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let path = manifest["spritesheetPath"] as? String,
            // The manifest names a file in its own folder, never a path elsewhere.
            !path.contains("/"), !path.hasPrefix("."),
            let source = CGImageSourceCreateWithURL(folder.appending(path: path) as CFURL, nil),
            let sheet = CGImageSourceCreateImageAtIndex(source, 0, nil),
            sheet.width == 1536, sheet.height == 1872
        else { return nil }
        return PetSprite(sheet: sheet)
    }
}

/// The pet, playing the row for bud's status. The frame comes from the clock and the time the
/// status began, so the view keeps no animation state of its own.
struct AnimatedPet: View {
    let sprite: PetSprite
    let status: PetStatus
    let since: Date
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let row = PetRow.of(status)
        TimelineView(.animation(minimumInterval: 0.04, paused: reduceMotion)) { context in
            let ms = max(0, Int(context.date.timeIntervalSince(since) * 1000))
            if let image = sprite.image(row: row, frame: reduceMotion ? 0 : row.frame(at: ms)) {
                Image(nsImage: image)
                    .interpolation(.high)
            }
        }
        .frame(width: PetSprite.cell.width / 2, height: PetSprite.cell.height / 2)
    }
}
