import AppKit
import SwiftUI

/// macOS vibrancy that stays vibrant. SwiftUI's materials follow the window's key state, and a
/// floating panel is almost never key, so they fall back to a flat grey. This pins the material
/// to its active look.
struct Vibrancy: NSViewRepresentable {
    var material: NSVisualEffectView.Material = .popover

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) {
        view.material = material
    }
}

extension View {
    /// A vibrant surface with a hairline edge, clipped to `shape`.
    func surface<S: InsettableShape>(_ shape: S, material: NSVisualEffectView.Material = .popover) -> some View {
        background(Vibrancy(material: material).clipShape(shape))
            .overlay(shape.strokeBorder(.primary.opacity(0.1), lineWidth: 0.5))
    }
}
