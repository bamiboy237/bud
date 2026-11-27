import AppKit
import SwiftUI

@MainActor
struct ChatInput: View {
    let title: String
    @Binding var text: String
    let focus: FocusState<Bool>.Binding
    let submit: () -> Void
    var onPasteImage: ((Data) -> Bool)? = nil
    @StateObject private var cursor = Cursor()

    private final class Cursor: ObservableObject {
        @Published var selection: TextSelection?
    }

    var body: some View {
        TextField(title, text: $text, selection: $cursor.selection, axis: .vertical)
            .textFieldStyle(.plain)
            .focused(focus)
            .onSubmit {
                if let event = NSApp.currentEvent, event.type == .keyDown,
                   event.modifierFlags.contains(.shift) {
                    let range = if case .selection(let range) = cursor.selection?.indices {
                        range
                    } else {
                        text.endIndex..<text.endIndex
                    }
                    let insertion = Self.insertingNewline(in: text, replacing: range)
                    text = insertion.text
                    cursor.selection = TextSelection(insertionPoint: insertion.caret)
                } else {
                    submit()
                }
            }
            .submitScope()
            .background {
                if let onPasteImage {
                    ImagePasteShortcut(focus: focus, paste: onPasteImage)
                }
            }
    }

    static func insertingNewline(
        in text: String, replacing range: Range<String.Index>
    ) -> (text: String, caret: String.Index) {
        let offset = range.lowerBound.utf16Offset(in: text)
        var edited = text
        edited.replaceSubrange(range, with: "\n")
        return (edited, String.Index(utf16Offset: offset + 1, in: edited))
    }
}

private struct ImagePasteShortcut: NSViewRepresentable {
    let focus: FocusState<Bool>.Binding
    let paste: (Data) -> Bool

    func makeNSView(context: Context) -> Monitor { Monitor() }

    func updateNSView(_ view: Monitor, context: Context) {
        view.paste = {
            guard focus.wrappedValue, let png = DraftAttachment.clipboardPNG(from: .general)
            else { return false }
            return paste(png)
        }
    }

    final class Monitor: NSView {
        var paste: (() -> Bool)?
        private var eventMonitor: Any?

        override func viewWillMove(toWindow newWindow: NSWindow?) {
            if let eventMonitor {
                NSEvent.removeMonitor(eventMonitor)
                self.eventMonitor = nil
            }
            super.viewWillMove(toWindow: newWindow)
            guard newWindow != nil else { return }
            // Read the shortcut before AppKit routes it through the Edit menu.
            eventMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                let modifiers = event.modifierFlags.intersection([.command, .control, .option, .shift])
                guard let self, let window, event.window === window, window.isKeyWindow,
                      modifiers == .command, event.charactersIgnoringModifiers?.lowercased() == "v",
                      paste?() == true else { return event }
                return nil
            }
        }
    }
}
