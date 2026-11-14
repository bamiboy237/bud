import Markdown
import SwiftUI

/// A reply drawn from its markdown tree: one view per block, so paragraphs get space between
/// them and list items hang off their markers. Inline styling (bold, code, links) becomes an
/// attributed string inside each block, so text wraps and links stay clickable.
///
/// The whole reply is parsed again on every streamed delta. Replies are short, and a fresh
/// parse means a half-written construct (an unclosed `**`) settles as soon as it closes.
struct MarkdownView: View {
    let blocks: [any Markup]

    init(_ text: String) {
        blocks = Array(Document(parsing: text).children)
    }

    var body: some View {
        Blocks(blocks: blocks, spacing: Metrics.paragraph)
    }
}

private enum Metrics {
    static let paragraph: CGFloat = 8
    static let item: CGFloat = 3
    static let marker: CGFloat = 16
}

private struct Blocks: View {
    let blocks: [any Markup]
    let spacing: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: spacing) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                Block(markup: block)
            }
        }
    }
}

private struct Block: View {
    let markup: any Markup

    var body: some View {
        switch markup {
        case let heading as Heading:
            prose(inline(heading), font: heading.level <= 2 ? .headline : .subheadline.weight(.semibold))
                .padding(.top, 2)
        case let paragraph as Paragraph:
            prose(inline(paragraph))
        case let list as UnorderedList:
            ListBlock(items: Array(list.listItems), start: nil)
        case let list as OrderedList:
            ListBlock(items: Array(list.listItems), start: Int(list.startIndex))
        case let code as CodeBlock:
            CodeBlockView(code: code.code)
        case let quote as BlockQuote:
            HStack(alignment: .top, spacing: 8) {
                RoundedRectangle(cornerRadius: 1).fill(.quaternary).frame(width: 2)
                Blocks(blocks: Array(quote.children), spacing: Metrics.paragraph)
                    .foregroundStyle(.secondary)
            }
            .fixedSize(horizontal: false, vertical: true)
        case let table as Markdown.Table:
            TableView(table: table)
        case is ThematicBreak:
            Divider().padding(.vertical, 2)
        default:
            // HTML and anything newer than this renderer: show the source rather than drop it.
            prose(AttributedString(markup.format()))
        }
    }
}

/// Body text, wrapped to the bubble's width and selectable.
private func prose(_ text: AttributedString, font: Font = .body) -> some View {
    Text(text)
        .font(font)
        .lineSpacing(2)
        .textSelection(.enabled)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
}

private struct ListBlock: View {
    let items: [ListItem]
    /// The first number of an ordered list; nil for bullets.
    let start: Int?

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.item) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                // The marker sits in its own column, so wrapped lines align with the text.
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    marker(index, item)
                        .frame(width: Metrics.marker, alignment: start == nil ? .center : .trailing)
                    Blocks(blocks: Array(item.children), spacing: Metrics.item)
                }
            }
        }
    }

    @ViewBuilder
    private func marker(_ index: Int, _ item: ListItem) -> some View {
        if let checkbox = item.checkbox {
            Image(systemName: checkbox == .checked ? "checkmark.square" : "square")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else if let start {
            Text("\(start + index).")
                .font(.body.monospacedDigit())
                .foregroundStyle(.secondary)
        } else {
            Text("•").font(.body).foregroundStyle(.tertiary)
        }
    }
}

private struct CodeBlockView: View {
    let code: String

    var body: some View {
        // Long lines wrap: sideways scrolling inside a small floating bubble is fiddly.
        Text(code.hasSuffix("\n") ? String(code.dropLast()) : code)
            .font(.system(size: 11.5, design: .monospaced))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
    }
}

private struct TableView: View {
    let table: Markdown.Table

    var body: some View {
        // Fits the bubble's width; cells wrap rather than scroll.
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
            GridRow { cells(Array(table.head.children), header: true) }
            Divider()
            ForEach(Array(table.body.children.enumerated()), id: \.offset) { _, row in
                GridRow { cells(Array(row.children), header: false) }
            }
        }
        .font(.callout)
    }

    @ViewBuilder
    private func cells(_ cells: [any Markup], header: Bool) -> some View {
        ForEach(Array(cells.enumerated()), id: \.offset) { _, cell in
            Text(inline(cell))
                .fontWeight(header ? .semibold : .regular)
                .foregroundStyle(header ? .secondary : .primary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// A block's inline children as one attributed string. Emphasis is carried as presentation
/// intent, which SwiftUI's Text draws, so bold inside a link inside a list item still works.
private func inline(_ markup: any Markup, intent: InlinePresentationIntent = []) -> AttributedString {
    var out = AttributedString()
    for child in markup.children {
        switch child {
        case let text as Markdown.Text:
            var run = AttributedString(text.string)
            if !intent.isEmpty { run.inlinePresentationIntent = intent }
            out += run
        case is Strong:
            out += inline(child, intent: intent.union(.stronglyEmphasized))
        case is Emphasis:
            out += inline(child, intent: intent.union(.emphasized))
        case is Strikethrough:
            out += inline(child, intent: intent.union(.strikethrough))
        case let code as InlineCode:
            var run = AttributedString(code.code)
            run.inlinePresentationIntent = intent.union(.code)
            out += run
        case let link as Markdown.Link:
            var run = inline(link, intent: intent)
            if let destination = link.destination, let url = URL(string: destination) {
                run.link = url
            }
            out += run
        case is SoftBreak:
            out += AttributedString(" ")
        case is LineBreak:
            out += AttributedString("\n")
        case let image as Markdown.Image:
            // No images in a bubble: their alt text stands in.
            out += inline(image, intent: intent)
        default:
            out += AttributedString(child.format())
        }
    }
    return out
}
