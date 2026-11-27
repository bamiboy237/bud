import AppKit
import SwiftUI
import Testing
@testable import BudCompanion

struct MaterialBorderTests {
    @Test(arguments: [1.0, 2.0], [false, true]) @MainActor
    func outlineDrawsOnlyInsideTheShape(scale: CGFloat, focused: Bool) throws {
        let pixels = try render(PaperOutline(shape: Rectangle(), color: .white, focused: focused), scale: scale)
        let edge = Int(2 * scale)
        let row = pixels.pixelsHigh / 2
        let outer = try #require(pixels.colorAt(x: edge - 1, y: row))
        let first = try #require(pixels.colorAt(x: edge, y: row))
        let second = try #require(pixels.colorAt(x: edge + 1, y: row))
        let third = try #require(pixels.colorAt(x: edge + 2, y: row))

        #expect(outer.alphaComponent == 0)
        #expect(first.alphaComponent == 1)
        #expect(second.alphaComponent == (focused ? 1 : 0))
        #expect(third.alphaComponent == 0)
        if focused {
            let color = try #require(first.usingColorSpace(.sRGB))
            #expect(abs(color.redComponent - color.greenComponent) < 0.01)
            #expect(abs(color.greenComponent - color.blueComponent) < 0.01)
        }
    }

    @Test @MainActor
    func raisedDarkPaperSurfaceDrawsOnlyTheRequestedBorder() throws {
        _ = AppearanceStore.shared
        let savedTheme = AppearanceStore.activeTheme
        defer { AppearanceStore.activeTheme = savedTheme }
        AppearanceStore.activeTheme = .paper
        let red = Color(red: 1, green: 0, blue: 0)
        let surface = Color.clear.surface(Rectangle(), fill: .clear, border: red)
            .environment(\.colorScheme, .dark)
        let pixels = try render(surface, scale: 2)
        let edge = try #require(pixels.colorAt(x: 4, y: pixels.pixelsHigh / 2)?.usingColorSpace(.sRGB))
        let singleOutline = PaperOutline(shape: Rectangle(), color: red).environment(\.colorScheme, .dark)
        let reference = try render(singleOutline, scale: 2)
        let expected = try #require(reference.colorAt(x: 4, y: reference.pixelsHigh / 2)?.usingColorSpace(.sRGB))

        #expect(edge.alphaComponent == 1)
        #expect(abs(edge.redComponent - expected.redComponent) < 0.01)
        #expect(abs(edge.greenComponent - expected.greenComponent) < 0.01)
        #expect(abs(edge.blueComponent - expected.blueComponent) < 0.01)
    }

    @MainActor
    private func render<V: View>(_ view: V, scale: CGFloat) throws -> NSBitmapImageRep {
        let renderer = ImageRenderer(content: view.frame(width: 12, height: 12)
            .padding(2)
            .environment(\.displayScale, scale))
        renderer.scale = scale
        return NSBitmapImageRep(cgImage: try #require(renderer.cgImage))
    }
}
