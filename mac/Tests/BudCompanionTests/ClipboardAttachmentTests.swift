import AppKit
import Testing
import UniformTypeIdentifiers
@testable import BudCompanion

@MainActor
struct ClipboardAttachmentTests {
    @Test
    func plainTextTakesPriorityOverAnImageRepresentation() throws {
        let png = try #require(try bitmap().representation(using: .png, properties: [:]))

        #expect(DraftAttachment.clipboardPNG(hasPlainText: true, png: png, tiff: nil) == nil)
        #expect(DraftAttachment.clipboardPNG(hasPlainText: false, png: png, tiff: nil) == png)
        #expect(DraftAttachment.clipboardPNG(hasPlainText: false, png: Data([0]), tiff: nil) == nil)
    }

    @Test
    func convertsTIFFAndNSImageToPNG() throws {
        let bitmap = try bitmap()
        let tiff = try #require(bitmap.tiffRepresentation)
        let image = NSImage(size: NSSize(width: 2, height: 1))
        image.addRepresentation(bitmap)

        let convertedTIFF = try #require(
            DraftAttachment.clipboardPNG(hasPlainText: false, png: Data([0]), tiff: tiff))
        let convertedImage = try #require(
            DraftAttachment.clipboardPNG(hasPlainText: false, png: nil, tiff: nil) { image })
        for data in [convertedTIFF, convertedImage] {
            #expect(DraftAttachment.clipboardPNG(hasPlainText: false, png: data, tiff: nil) == data)
            let decoded = try #require(NSBitmapImageRep(data: data))
            #expect(decoded.pixelsWide == 2)
            #expect(decoded.pixelsHigh == 1)
        }
    }

    @Test
    func stagesScreenshotsWithUniquePNGNamesAndReadsThemAsFiles() throws {
        let png = try #require(try bitmap().representation(using: .png, properties: [:]))
        let screenshot = try DraftAttachment.stageScreenshot(png, existingNames: ["briefing.txt"])
        defer { try? FileManager.default.removeItem(at: screenshot.deletingLastPathComponent()) }
        let duplicate = try DraftAttachment.stageScreenshot(png, existingNames: ["screenshot.PNG"])
        defer { try? FileManager.default.removeItem(at: duplicate.deletingLastPathComponent()) }

        #expect(screenshot.lastPathComponent == "Screenshot.png")
        #expect(duplicate.lastPathComponent == "Screenshot 2.png")
        let file = try DraftAttachment.read(duplicate)
        #expect(file.reference.name == "Screenshot 2.png")
        #expect(file.reference.type == .png)
        #expect(file.data == png)
    }

    @Test
    func pastedScreenshotsUseTheFileSizeAndBatchLimits() async throws {
        let companion = Companion()
        companion.draft = "Briefing draft"
        let png = try #require(try bitmap().representation(using: .png, properties: [:]))

        #expect(companion.addPastedImage(Data(count: DraftAttachment.limit + 1)))
        try await finishReading(companion)
        #expect(companion.draftAttachments.isEmpty)
        #expect(companion.notices.contains { $0.text.contains("up to 25 MiB") })

        for _ in 0..<DraftAttachment.maxCount {
            #expect(companion.addPastedImage(png))
            try await finishReading(companion)
        }
        #expect(companion.addPastedImage(png))
        #expect(!companion.readingAttachments)
        #expect(companion.draftAttachments.count == DraftAttachment.maxCount)
        #expect(companion.draftAttachments.prefix(2).map(\.reference.name) == ["Screenshot.png", "Screenshot 2.png"])
        #expect(companion.notices.contains { $0.text.contains("up to five files") })
        #expect(companion.draft == "Briefing draft")
    }

    private func bitmap() throws -> NSBitmapImageRep {
        let context = try #require(CGContext(
            data: nil, width: 2, height: 1, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.setFillColor(red: 1, green: 0, blue: 0, alpha: 1)
        context.fill(CGRect(x: 0, y: 0, width: 2, height: 1))
        return NSBitmapImageRep(cgImage: try #require(context.makeImage()))
    }

    private func finishReading(_ companion: Companion) async throws {
        let deadline = ContinuousClock.now + .seconds(5)
        while companion.readingAttachments, ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(5))
        }
        try #require(!companion.readingAttachments)
    }
}
