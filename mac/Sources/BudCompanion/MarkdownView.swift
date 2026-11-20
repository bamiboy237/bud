import Markdown
import SwiftUI

/// A reply drawn from its markdown tree: one view per block, so paragraphs get space between
/// them and list items hang off their markers. Inline styling (bold, code, links) becomes an
/// attributed string inside each block, so text wraps and links stay clickable.
///
/// Changed text is parsed afresh, so an unfinished construct settles as soon as it closes.
/// Unchanged replies reuse their parse when the conversation is rebuilt.
struct MarkdownView: View {
    let blocks: [any Markup]

    init(_ text: String) {
        blocks = ParsedMarkdown.blocks(text)
    }

    var body: some View {
        Blocks(blocks: blocks, spacing: Metrics.paragraph)
    }
}

@MainActor
private enum ParsedMarkdown {
    private final class Entry {
        let blocks: [any Markup]
        init(_ blocks: [any Markup]) { self.blocks = blocks }
    }

    private static let cache: NSCache<NSString, Entry> = {
        let cache = NSCache<NSString, Entry>()
        cache.countLimit = 256
        return cache
    }()

    static func blocks(_ text: String) -> [any Markup] {
        let key = text as NSString
        if let entry = cache.object(forKey: key) { return entry.blocks }
        let blocks = Array(Document(parsing: text).children)
        cache.setObject(Entry(blocks), forKey: key)
        return blocks
    }
}

/// One type scale for replies, in SF Pro, built from a single body size: the bubble reads at
/// 13 pt, the conversation window a step up. Headings, lists, tables, and code all derive
/// from it, so nothing in a reply picks its own typeface or size.
struct ReplyType {
    var size: CGFloat = 13
    var lineSpacing: CGFloat = 2.5

    var body: Font { .system(size: size) }
    var title: Font { .system(size: size + 3, weight: .semibold) }
    var heading: Font { .system(size: size + 1, weight: .semibold) }
    var section: Font { .system(size: size, weight: .semibold) }
    var marker: Font { .system(size: size).monospacedDigit() }
    var table: Font { .system(size: size - 1) }
    var code: Font { .system(size: size - 1, design: .monospaced) }
}

private struct ReplyTypeKey: EnvironmentKey {
    static let defaultValue = ReplyType()
}

extension EnvironmentValues {
    var replyType: ReplyType {
        get { self[ReplyTypeKey.self] }
        set { self[ReplyTypeKey.self] = newValue }
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
    @Environment(\.replyType) private var type

    var body: some View {
        switch markup {
        case let heading as Heading:
            // Three steps of the same family, in the reply's own sentence case.
            prose(inline(heading), font: heading.level == 1 ? type.title
                  : heading.level == 2 ? type.heading : type.section)
                .padding(.top, heading.level <= 2 ? 4 : 2)
        case let paragraph as Paragraph:
            Prose(text: inline(paragraph, code: type.code))
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
                    .foregroundStyle(Paper.inkSecondary)
            }
            .fixedSize(horizontal: false, vertical: true)
        case let table as Markdown.Table:
            TableView(table: table)
        case is ThematicBreak:
            Divider().padding(.vertical, 2)
        default:
            // HTML and anything newer than this renderer: show the source rather than drop it.
            Prose(text: AttributedString(markup.format()))
        }
    }
}

/// Heading text, wrapped to the bubble's width and selectable.
private func prose(_ text: AttributedString, font: Font) -> some View {
    Text(text)
        .font(font)
        .lineSpacing(2)
        .textSelection(.enabled)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
}

/// Body text in the reply typeface.
private struct Prose: View {
    let text: AttributedString
    @Environment(\.replyType) private var type

    var body: some View {
        Text(text)
            .font(type.body)
            .lineSpacing(type.lineSpacing)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ListBlock: View {
    let items: [ListItem]
    /// The first number of an ordered list; nil for bullets.
    let start: Int?
    @Environment(\.replyType) private var type

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.item) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                // The marker sits in its own column, so wrapped lines align with the text.
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    marker(index, item)
                        .fixedSize()
                        .frame(width: markerWidth, alignment: start == nil ? .center : .trailing)
                    Blocks(blocks: Array(item.children), spacing: Metrics.item)
                }
            }
        }
    }

    /// Wide enough for the longest number in the list ("12."), so every marker fits on one
    /// line and the items stay aligned. Body digits are about 7.5 pt; the period about 4.
    private var markerWidth: CGFloat {
        guard let start else { return Metrics.marker }
        let digits = String(start + max(items.count - 1, 0)).count
        return max(Metrics.marker, CGFloat(digits) * type.size * 0.58 + 5)
    }

    @ViewBuilder
    private func marker(_ index: Int, _ item: ListItem) -> some View {
        if let checkbox = item.checkbox {
            Image(systemName: checkbox == .checked ? "checkmark.square" : "square")
                .font(type.body)
                .foregroundStyle(Paper.inkSecondary)
        } else if let start {
            Text("\(start + index).")
                .font(type.marker)
                .foregroundStyle(Paper.inkSecondary)
        } else {
            Text("•").font(type.body).foregroundStyle(Paper.inkSecondary)
        }
    }
}

private struct CodeBlockView: View {
    let code: String
    @Environment(\.replyType) private var type

    var body: some View {
        // Long lines wrap: sideways scrolling inside a small floating bubble is fiddly.
        Text(code.hasSuffix("\n") ? String(code.dropLast()) : code)
            .font(type.code)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
    }
}

private struct TableView: View {
    let table: Markdown.Table
    @Environment(\.replyType) private var type

    var body: some View {
        // Fits the bubble's width; cells wrap rather than scroll.
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
            GridRow { cells(Array(table.head.children), header: true) }
            Divider()
            ForEach(Array(table.body.children.enumerated()), id: \.offset) { _, row in
                GridRow { cells(Array(row.children), header: false) }
            }
        }
        .font(type.table)
    }

    @ViewBuilder
    private func cells(_ cells: [any Markup], header: Bool) -> some View {
        ForEach(Array(cells.enumerated()), id: \.offset) { _, cell in
            Text(inline(cell, code: .system(size: type.size - 2, design: .monospaced)))
                .fontWeight(header ? .semibold : .regular)
                .foregroundStyle(header ? Paper.inkSecondary : Paper.inkPrimary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// A block's inline children as one attributed string. Emphasis is carried as presentation
/// intent, which SwiftUI's Text draws, so bold inside a link inside a list item still works.
/// `code` sets inline code a step below the text around it, so it does not look heavier.
private func inline(
    _ markup: any Markup, intent: InlinePresentationIntent = [], code codeFont: Font? = nil
) -> AttributedString {
    var out = AttributedString()
    for child in markup.children {
        switch child {
        case let text as Markdown.Text:
            var run = AttributedString(text.string)
            if !intent.isEmpty { run.inlinePresentationIntent = intent }
            out += run
        case is Strong:
            out += inline(child, intent: intent.union(.stronglyEmphasized), code: codeFont)
        case is Emphasis:
            out += inline(child, intent: intent.union(.emphasized), code: codeFont)
        case is Strikethrough:
            out += inline(child, intent: intent.union(.strikethrough), code: codeFont)
        case let code as InlineCode:
            var run = AttributedString(code.code)
            if let codeFont {
                run.inlinePresentationIntent = intent.isEmpty ? nil : intent
                run.font = codeFont
            } else {
                run.inlinePresentationIntent = intent.union(.code)
            }
            out += run
        case let link as Markdown.Link:
            var run = inline(link, intent: intent, code: codeFont)
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
            out += inline(image, intent: intent, code: codeFont)
        default:
            out += AttributedString(child.format())
        }
    }
    return out
}
