import AppKit
import SwiftUI

@MainActor
struct ChatInput: View {
    let title: String
    @Binding var text: String
    let focus: FocusState<Bool>.Binding
    let submit: () -> Void
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
