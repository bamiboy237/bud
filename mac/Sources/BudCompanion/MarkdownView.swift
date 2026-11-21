import AppKit
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
    @Environment(\.replyType) private var type

    init(_ text: String) {
        blocks = ParsedMarkdown.blocks(text)
    }

    var body: some View {
        // Links and citations take the one accent, scoped here so pickers elsewhere keep theirs.
        Blocks(blocks: blocks, spacing: type.blockSpacing)
            .tint(Paper.accent)
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
/// from it, so nothing in a reply picks its own typeface or size. SF Mono is wider and taller
/// than SF Pro at the same size, so code sits at about 0.86 of the body to look the same.
struct ReplyType {
    var size: CGFloat = 13
    var lineSpacing: CGFloat = 3
    /// Space between blocks, and between list items.
    var blockSpacing: CGFloat = 9
    var itemSpacing: CGFloat = 4
    /// The floating bubble: code wraps instead of scrolling sideways.
    var compact: Bool = true

    var body: Font { .system(size: size) }
    var title: Font { .system(size: size + 4.5, weight: .semibold) }
    var heading: Font { .system(size: size + 2, weight: .semibold) }
    var section: Font { .system(size: size + 0.5, weight: .semibold) }
    var minor: Font { .system(size: size, weight: .medium) }
    var marker: Font { .system(size: size - 0.5).monospacedDigit() }
    var table: Font { .system(size: size - 1.5).monospacedDigit() }
    var codeSize: CGFloat { (size * 0.86).rounded() }
    var code: Font { .system(size: codeSize, design: .monospaced) }
    var codeLineSpacing: CGFloat { compact ? 2 : 2.5 }
    var meta: Font { .system(size: max(10.5, size - 2.5), weight: .medium, design: .monospaced) }
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
    static let marker: CGFloat = 16
    static let radius: CGFloat = 6
}

private struct Blocks: View {
    let blocks: [any Markup]
    let spacing: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: spacing) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                Block(markup: block, first: index == 0)
            }
        }
    }
}

private struct Block: View {
    let markup: any Markup
    let first: Bool
    @Environment(\.replyType) private var type

    var body: some View {
        switch markup {
        case let heading as Heading:
            // More room above than below, so a heading belongs to the text it introduces.
            VStack(alignment: .leading, spacing: 5) {
                Prose(text: inline(heading, code: type.code), font: font(heading.level), spacing: 1.5)
                    .foregroundStyle(heading.level >= 4 ? Paper.inkSecondary : Paper.inkPrimary)
                // Top-level headings carry a hairline, so a long reply reads in sections.
                if heading.level <= 2 {
                    Rectangle().fill(Paper.hairline).frame(height: 0.5)
                }
            }
            .padding(.top, first ? 0 : heading.level <= 2 ? type.blockSpacing * 0.6 : 2)
        case let paragraph as Paragraph:
            Prose(text: inline(paragraph, code: type.code))
        case let list as UnorderedList:
            ListBlock(items: Array(list.listItems), start: nil)
        case let list as OrderedList:
            ListBlock(items: Array(list.listItems), start: Int(list.startIndex))
        case let code as CodeBlock:
            CodeBlockView(code: code.code, language: code.language)
        case let quote as BlockQuote:
            HStack(alignment: .top, spacing: 10) {
                RoundedRectangle(cornerRadius: 1.5).fill(Paper.inkRule).frame(width: 3)
                Blocks(blocks: Array(quote.children), spacing: type.itemSpacing + 2)
                    .foregroundStyle(Paper.inkSecondary)
                    .padding(.vertical, 1)
            }
            .fixedSize(horizontal: false, vertical: true)
        case let table as Markdown.Table:
            TableView(table: table)
        case is ThematicBreak:
            Rectangle().fill(Paper.hairline).frame(height: 0.5).padding(.vertical, 4)
        default:
            // HTML and anything newer than this renderer: show the source rather than drop it.
            Prose(text: AttributedString(markup.format()))
        }
    }

    private func font(_ level: Int) -> Font {
        switch level {
        case 1: type.title
        case 2: type.heading
        case 3: type.section
        default: type.minor
        }
    }
}

/// Text in the reply typeface, wrapped and selectable, with inline code drawn as chips.
private struct Prose: View {
    let text: AttributedString
    var font: Font?
    var spacing: CGFloat?
    @Environment(\.replyType) private var type

    var body: some View {
        chipped(text)
            .font(font ?? type.body)
            .lineSpacing(spacing ?? type.lineSpacing)
            .textRenderer(CodeChips(fill: Paper.inkPrimary.opacity(0.085)))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Marks a run that is inline code, so `CodeChips` can draw its chip.
private enum CodeRun: AttributedStringKey {
    typealias Value = Bool
    static let name = "bud.codeRun"
}

private struct CodeChipAttribute: TextAttribute {}

/// One Text from an attributed string, with each code run tagged for a chip.
private func chipped(_ text: AttributedString) -> SwiftUI.Text {
    var out = SwiftUI.Text(verbatim: "")
    for run in text.runs {
        let piece = SwiftUI.Text(AttributedString(text[run.range]))
        out = out + (run[CodeRun.self] == true ? piece.customAttribute(CodeChipAttribute()) : piece)
    }
    return out
}

/// Draws a rounded chip behind inline code, like an editor's: a 4 pt corner and a faint ink
/// fill, a little wider than the glyphs so the code does not touch its neighbours.
private struct CodeChips: TextRenderer {
    let fill: Color

    func draw(layout: SwiftUI.Text.Layout, in context: inout GraphicsContext) {
        for line in layout {
            for run in line {
                if run[CodeChipAttribute.self] != nil {
                    let rect = run.typographicBounds.rect.insetBy(dx: -3, dy: -1)
                    context.fill(
                        RoundedRectangle(cornerRadius: 4, style: .continuous).path(in: rect),
                        with: .color(fill))
                }
                context.draw(run)
            }
        }
    }
}

private struct ListBlock: View {
    let items: [ListItem]
    /// The first number of an ordered list; nil for bullets.
    let start: Int?
    @Environment(\.replyType) private var type

    var body: some View {
        VStack(alignment: .leading, spacing: type.itemSpacing) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                // The marker sits in its own column, so wrapped lines align with the text.
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    marker(index, item)
                        .fixedSize()
                        .frame(width: markerWidth, alignment: start == nil ? .center : .trailing)
                    Blocks(blocks: Array(item.children), spacing: type.itemSpacing)
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
            Text("•").font(type.body).foregroundStyle(Paper.inkMuted)
        }
    }
}

/// A code block on recessed paper with a hairline edge. In the window, long lines scroll
/// sideways so code keeps its shape; in the small bubble they wrap. Hovering shows the
/// language and a copy button.
private struct CodeBlockView: View {
    let code: String
    let language: String?
    @Environment(\.replyType) private var type
    @StateObject private var hover = Flag()
    @StateObject private var copied = Flag()

    private var source: String { code.hasSuffix("\n") ? String(code.dropLast()) : code }

    var body: some View {
        content
            .padding(.horizontal, 10)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            // Lighter than the page, like an editor pane set into the reply.
            .background(Paper.elevated, in: shape)
            .overlay(shape.strokeBorder(Paper.hairline, lineWidth: 0.5))
            .overlay(alignment: .topTrailing) { toolbar }
            .onHover { hover.on = $0 }
    }

    private var shape: RoundedRectangle {
        RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
    }

    @ViewBuilder
    private var content: some View {
        let text = Text(Syntax.highlight(source, language: language))
            .font(type.code)
            .lineSpacing(type.codeLineSpacing)
            .foregroundStyle(Paper.inkPrimary)
            .textSelection(.enabled)
        if type.compact {
            text.fixedSize(horizontal: false, vertical: true)
        } else {
            ScrollView(.horizontal, showsIndicators: false) {
                text.fixedSize()
            }
        }
    }

    @ViewBuilder
    private var toolbar: some View {
        if hover.on || copied.on {
            HStack(spacing: 6) {
                if let language, !language.isEmpty {
                    Text(language.lowercased())
                        .font(type.meta)
                        .foregroundStyle(Paper.inkMuted)
                }
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(source, forType: .string)
                    copied.on = true
                    Task { @MainActor in
                        try? await Task.sleep(for: .seconds(1.2))
                        copied.on = false
                    }
                } label: {
                    Image(systemName: copied.on ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(Paper.inkSecondary)
                        .frame(width: 20, height: 18)
                        .background(Paper.elevated, in: RoundedRectangle(cornerRadius: 4))
                        .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Paper.hairline, lineWidth: 0.5))
                }
                .buttonStyle(.plain)
                .help(copied.on ? "Copied" : "Copy code")
                .accessibilityLabel("Copy code")
            }
            .padding(5)
            .transition(.opacity)
        }
    }
}

/// A table in a hairline frame: a tinted header row, row dividers, padded cells, and the
/// column alignment the markdown asked for. Numbers use tabular digits, so columns line up.
private struct TableView: View {
    let table: Markdown.Table
    @Environment(\.replyType) private var type

    var body: some View {
        // As wide as its content, like an editor's table; a table too wide for the space
        // takes the full width instead and its cells wrap.
        ViewThatFits(in: .horizontal) {
            grid.fixedSize(horizontal: true, vertical: true)
            grid.fixedSize(horizontal: false, vertical: true)
        }
    }

    private var grid: some View {
        Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
            GridRow { cells(Array(table.head.children), header: true) }
                .background(Paper.recessed)
            ForEach(Array(table.body.children.enumerated()), id: \.offset) { _, row in
                Rectangle().fill(Paper.hairline).frame(height: 0.5).gridCellUnsizedAxes(.horizontal)
                GridRow { cells(Array(row.children), header: false) }
            }
        }
        .font(type.table)
        .clipShape(RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius, style: .continuous)
                .strokeBorder(Paper.hairline, lineWidth: 0.5))
    }

    @ViewBuilder
    private func cells(_ cells: [any Markup], header: Bool) -> some View {
        ForEach(Array(cells.enumerated()), id: \.offset) { index, cell in
            let alignment = alignment(index)
            Text(inline(cell, code: .system(size: type.codeSize - 1, design: .monospaced)))
                .fontWeight(header ? .semibold : .regular)
                .foregroundStyle(header ? Paper.inkSecondary : Paper.inkPrimary)
                .multilineTextAlignment(alignment.text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                // Fill the column, so dividers and the header tint line up on every row.
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: alignment.frame)
                // A full grid, as in an editor: a hairline between columns too.
                .overlay(alignment: .leading) {
                    if index > 0 { Rectangle().fill(Paper.hairline).frame(width: 0.5) }
                }
                .gridColumnAlignment(alignment.horizontal)
        }
    }

    private func alignment(_ column: Int) -> (text: TextAlignment, frame: Alignment, horizontal: HorizontalAlignment) {
        let columns = table.columnAlignments
        switch column < columns.count ? columns[column] : nil {
        case .center: return (.center, .center, .center)
        case .right: return (.trailing, .trailing, .trailing)
        default: return (.leading, .leading, .leading)
        }
    }
}

/// A block's inline children as one attributed string. Emphasis is carried as presentation
/// intent, which SwiftUI's Text draws, so bold inside a link inside a list item still works.
/// `code` sets inline code at the code size, marked so it gets a chip.
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
            run[CodeRun.self] = true
            // Hair spaces outside the chip keep it off the words beside it.
            out += AttributedString("\u{200A}") + run + AttributedString("\u{200A}")
        case let link as Markdown.Link:
            var run = inline(link, intent: intent, code: codeFont)
            if let destination = link.destination, let url = URL(string: destination) {
                run.link = url
                if url.scheme == Citations.scheme {
                    // A citation is metadata: small mono, in the accent, never underlined.
                    run.font = .system(size: 11, weight: .medium, design: .monospaced)
                } else {
                    run.underlineStyle = .single
                }
            }
            out += run
        case is SoftBreak:
            // A single newline in a reply is meant as one: lists of lines, addresses, steps.
            out += AttributedString("\n")
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
