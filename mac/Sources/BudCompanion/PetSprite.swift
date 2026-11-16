import AppKit
import SwiftUI

/// What the pet is doing, in bud's terms. Each sprite decides how to show it.
///
/// The first five follow bud's status. The rest are brief modes on top of it (being carried,
/// listening while the talk key is held, a hop or a wave): they never change the status, and
/// when they end the status pose is back.
enum PetMove: Hashable, Sendable {
    case idle, working, needsYou, done, failed
    case walkLeft, walkRight, listen, hop, wave

    static func of(_ status: PetStatus) -> PetMove {
        switch status {
        case .idle: .idle
        case .working: .working
        case .needsYou: .needsYou
        case .done: .done
        case .failed: .failed
        }
    }
}

/// One animation: where each frame sits in the sheet, how long it shows, and whether the
/// animation loops or plays once and holds its last frame.
struct PetAction: Sendable {
    let frames: [CGRect]
    let durations: [Int]
    let loop: Bool

    var totalMs: Int { durations.reduce(0, +) }

    /// The frame `ms` into the animation: looping, or held on the last once it's done.
    func frame(at ms: Int) -> CGRect {
        let total = max(totalMs, 1)
        var t = loop ? ms % total : min(ms, total - 1)
        for (index, duration) in durations.enumerated() {
            if t < duration { return frames[index] }
            t -= duration
        }
        return frames[frames.count - 1]
    }
}

/// The facts the pet's view needs, from the companion. Which frame they mean depends on the
/// sprite's timings, so the view decides; see `PetSprite.frame(for:at:)`.
struct PetPose {
    var status: PetMove
    var statusSince: Date
    var walk: (move: PetMove, since: Date)?
    var listeningSince: Date?
    var reaction: (move: PetMove, since: Date)?
    var reactionAtStatusStart: (move: PetMove, since: Date)?
}

/// A pet's art, cut from its sheet as needed. The app bundles the default dog.
/// Users can override a pet in ~/Library/Application Support/bud/pets/<id>/.
/// Missing art falls back to a paw.
///
/// Two formats load:
/// - the Codex pet format: `pet.json` and an 8 by 9 sheet of 192 by 208 px cells, with the
///   row layout fixed by the format;
/// - a manifest (`manifest.json`, or `assets/manifest.json`): a sheet of any size, with named
///   actions, each listing its frames, per-frame durations and whether it loops, and a map
///   from bud's statuses to actions.
@MainActor
final class PetSprite {
    /// A frame's source size in pixels. Rendering fits the panel's fixed pet box.
    let cell: CGSize
    private let sheet: CGImage
    private let actions: [PetMove: PetAction]
    private var images: [String: NSImage] = [:]
    private static var cache: [String: PetSprite?] = [:]

    private init(sheet: CGImage, cell: CGSize, actions: [PetMove: PetAction]) {
        self.sheet = sheet
        self.cell = cell
        self.actions = actions
    }

    static func named(_ id: String) -> PetSprite? {
        if let hit = cache[id] { return hit }
        let sprite = folders(id).lazy.compactMap { loadManifest($0) ?? loadCodex($0) }.first
        cache[id] = sprite
        return sprite
    }

    var displaySize: CGSize { PetPanel.pet }

    // MARK: Choosing the frame

    /// The frame for this moment. In order: being carried, listening, a reaction still
    /// playing, then the status pose. A status animation that plays once (a thumbs-up when
    /// done, a sigh when it failed) plays when it's first seen and then holds its last frame
    /// until the status changes; a reaction that covered its start makes it play afterwards,
    /// and one that came after it finished leaves it settled.
    func frame(for pose: PetPose, at now: Date) -> CGRect? {
        func ms(since start: Date) -> Int { max(0, Int(now.timeIntervalSince(start) * 1000)) }

        if let walk = pose.walk, let action = actions[walk.move] {
            return action.frame(at: ms(since: walk.since))
        }
        if let since = pose.listeningSince, let action = actions[.listen] {
            return action.frame(at: ms(since: since))
        }
        if let reaction = pose.reaction, let action = actions[reaction.move],
           ms(since: reaction.since) < action.totalMs
        {
            return action.frame(at: ms(since: reaction.since))
        }
        guard let status = actions[pose.status] else { return nil }
        if status.loop { return status.frame(at: ms(since: pose.statusSince)) }
        var start = pose.statusSince
        if let reaction = pose.reactionAtStatusStart ?? pose.reaction, let action = actions[reaction.move] {
            let reactionEnd = reaction.since.addingTimeInterval(Double(action.totalMs) / 1000)
            if reaction.since <= pose.statusSince, reactionEnd > pose.statusSince { start = reactionEnd }
        }
        return status.frame(at: ms(since: start))
    }

    func image(_ rect: CGRect) -> NSImage? {
        let key = "\(Int(rect.minX)),\(Int(rect.minY))"
        if let hit = images[key] { return hit }
        guard let cut = sheet.cropping(to: rect) else { return nil }
        let image = NSImage(cgImage: cut, size: NSSize(width: rect.width / 2, height: rect.height / 2))
        images[key] = image
        return image
    }

    /// The idle pose as a small still, for places like the menu bar.
    func icon(height: CGFloat) -> NSImage? {
        guard let rect = actions[.idle]?.frames.first, let cut = sheet.cropping(to: rect) else { return nil }
        return NSImage(cgImage: cut, size: NSSize(width: height * rect.width / rect.height, height: height))
    }

    // MARK: Loading

    private static func folders(_ id: String) -> [URL] {
        var found: [URL] = []
        if let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first {
            found.append(support.appending(path: "bud/pets/\(id)"))
        }
        if let resources = Bundle.main.resourceURL {
            found.append(resources.appending(path: "pets/\(id)"))
        }
        let dev = URL(filePath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appending(path: "Resources/pets/\(id)")
        found.append(dev)
        return found
    }

    /// A file named by a manifest must sit in the manifest's own folder, never elsewhere.
    private static func image(named name: String, in folder: URL) -> CGImage? {
        guard !name.isEmpty, !name.contains("/"), !name.hasPrefix(".") else { return nil }
        let home = folder.resolvingSymlinksInPath().standardizedFileURL
        let path = home.appending(path: name).resolvingSymlinksInPath().standardizedFileURL
        guard path.deletingLastPathComponent().path == home.path,
              let source = CGImageSourceCreateWithURL(path as CFURL, nil)
        else { return nil }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }

    private static func json(_ url: URL) -> [String: Any]? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    /// The Codex format: rows in a fixed order, each a loop with a held last frame.
    private static func loadCodex(_ folder: URL) -> PetSprite? {
        guard let manifest = json(folder.appending(path: "pet.json")),
              let path = manifest["spritesheetPath"] as? String,
              let sheet = image(named: path, in: folder),
              sheet.width == 1536, sheet.height == 1872
        else { return nil }
        let cell = CGSize(width: 192, height: 208)
        // Row, frames, ms per frame, ms on the last frame, loops (from openai/codex#20863).
        func row(_ index: Int, _ count: Int, _ frameMs: Int, _ lastMs: Int, loop: Bool = true) -> PetAction {
            PetAction(
                frames: (0..<count).map {
                    CGRect(x: CGFloat($0) * cell.width, y: CGFloat(index) * cell.height,
                           width: cell.width, height: cell.height)
                },
                durations: Array(repeating: frameMs, count: count - 1) + [lastMs],
                loop: loop)
        }
        let waiting = row(6, 6, 150, 260)
        let actions: [PetMove: PetAction] = [
            .idle: row(0, 6, 140, 320),
            .walkRight: row(1, 8, 95, 95),
            .walkLeft: row(2, 8, 95, 95),
            .wave: row(3, 4, 140, 280, loop: false),
            .hop: row(4, 5, 110, 180, loop: false),
            .failed: row(5, 8, 140, 240),
            .needsYou: waiting,
            .listen: waiting,
            .working: row(7, 6, 120, 220),
            .done: row(8, 6, 150, 280),
        ]
        return PetSprite(sheet: sheet, cell: cell, actions: actions)
    }

    /// The manifest format: named actions, and a map from bud's statuses to them.
    static func loadManifest(_ folder: URL) -> PetSprite? {
        let candidates = [folder.appending(path: "manifest.json"), folder.appending(path: "assets/manifest.json")]
        guard let (manifest, home) = candidates.lazy.compactMap({ url in json(url).map { ($0, url.deletingLastPathComponent()) } }).first,
              (manifest["version"] as? Int) == 1,
              let atlas = manifest["atlas"] as? String,
              let sheet = image(named: atlas, in: home),
              let cellSize = manifest["cell"] as? [Int], cellSize.count == 2,
              cellSize.allSatisfy({ $0 > 0 }),
              let named = manifest["actions"] as? [String: Any],
              let states = manifest["states"] as? [String: Any]
        else { return nil }
        let bounds = CGRect(x: 0, y: 0, width: sheet.width, height: sheet.height)

        func action(_ id: String) -> PetAction? {
            guard let entry = named[id] as? [String: Any],
                  let frames = entry["frames"] as? [[String: Any]], !frames.isEmpty
            else { return nil }
            var rects: [CGRect] = []
            var durations: [Int] = []
            var totalDuration = 0
            for frame in frames {
                guard let r = frame["rect"] as? [Int], r.count == 4,
                      r[0] >= 0, r[1] >= 0, r[2] == cellSize[0], r[3] == cellSize[1],
                      let duration = frame["durationMs"] as? Int, duration > 0
                else { return nil }
                let rect = CGRect(x: r[0], y: r[1], width: r[2], height: r[3])
                // A frame outside the sheet means a broken manifest: skip the whole action.
                guard bounds.contains(rect) else { return nil }
                let (nextDuration, overflow) = totalDuration.addingReportingOverflow(duration)
                guard !overflow else { return nil }
                totalDuration = nextDuration
                rects.append(rect)
                durations.append(duration)
            }
            return PetAction(frames: rects, durations: durations, loop: entry["loop"] as? Bool ?? true)
        }

        var actions: [PetMove: PetAction] = [:]
        let statuses: [(PetMove, String)] = [
            (.idle, "idle"), (.working, "working"), (.needsYou, "needsYou"), (.done, "done"), (.failed, "failed"),
        ]
        for (move, key) in statuses {
            if let id = states[key] as? String, let found = action(id) { actions[move] = found }
        }
        guard let idle = actions[.idle] else { return nil }
        for (move, _) in statuses where actions[move] == nil {
            actions[move] = idle
        }
        let modes: [(PetMove, String)] = [
            (.walkLeft, "walk-left"), (.walkRight, "walk-right"), (.hop, "hop"),
            (.wave, "wave"), (.listen, "listen"),
        ]
        for (move, id) in modes {
            if let found = action(id) { actions[move] = found }
        }
        return PetSprite(
            sheet: sheet, cell: CGSize(width: cellSize[0], height: cellSize[1]), actions: actions)
    }
}

/// The pet, showing whichever frame the sprite picks for the moment. The frame comes from the
/// clock and the companion's facts, so the view keeps no animation state of its own.
/// Reduced motion: each pose's first frame, still.
struct AnimatedPet: View {
    let sprite: PetSprite
    let size: CGSize?
    let pose: () -> PetPose
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(sprite: PetSprite, size: CGSize? = nil, pose: @escaping () -> PetPose) {
        self.sprite = sprite
        self.size = size
        self.pose = pose
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 0.04, paused: reduceMotion)) { context in
            let pose = pose()
            let moment = reduceMotion ? Date.distantPast : context.date
            if let rect = sprite.frame(for: still(pose), at: moment),
               let image = sprite.image(rect)
            {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .scaledToFit()
            }
        }
        .frame(width: size?.width ?? sprite.displaySize.width, height: size?.height ?? sprite.displaySize.height)
    }

    /// With reduced motion every animation starts "now", so the first frame shows and stays.
    private func still(_ pose: PetPose) -> PetPose {
        guard reduceMotion else { return pose }
        let start = Date.distantPast
        return PetPose(
            status: pose.status, statusSince: start,
            walk: pose.walk.map { ($0.move, start) },
            listeningSince: pose.listeningSince.map { _ in start },
            reaction: nil)
    }
}
