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
    private let parsed: ParsedMarkdown.Entry
    let rich: Bool
    @Environment(\.replyType) private var type

    init(_ text: String, rich: Bool = false) {
        self.rich = rich
        parsed = ParsedMarkdown.parse(text)
    }

    var body: some View {
        // Links and citations take the one accent, scoped here so pickers elsewhere keep theirs.
        Blocks(blocks: parsed.blocks, spacing: type.blockSpacing, rich: rich)
            .environment(\.markdownEquations, parsed.equations)
            .tint(Paper.accent)
    }
}

@MainActor
private enum ParsedMarkdown {
    final class Entry {
        let blocks: [any Markup]
        let equations: [Character: MarkdownEquation]

        init(_ text: String) {
            let document = Document(parsing: text)
            if text.contains("$") || text.contains("\\(") || text.contains("\\[") {
                let math = MarkdownMath(text, document: document)
                equations = math.equations
                blocks = Array((equations.isEmpty ? document : Document(parsing: math.source)).children)
            } else {
                equations = [:]
                blocks = Array(document.children)
            }
        }
    }

    private static let cache: NSCache<NSString, Entry> = {
        let cache = NSCache<NSString, Entry>()
        cache.countLimit = 256
        return cache
    }()

    static func parse(_ text: String) -> Entry {
        let key = text as NSString
        if let entry = cache.object(forKey: key) { return entry }
        let entry = Entry(text)
        cache.setObject(entry, forKey: key)
        return entry
    }
}

private struct MarkdownEquationsKey: EnvironmentKey {
    static let defaultValue: [Character: MarkdownEquation] = [:]
}

private extension EnvironmentValues {
    var markdownEquations: [Character: MarkdownEquation] {
        get { self[MarkdownEquationsKey.self] }
        set { self[MarkdownEquationsKey.self] = newValue }
    }
}

/// One type scale for replies. The bubble reads at 13 pt and the conversation window at 14 pt.
struct ReplyType {
    var size: CGFloat = 13
    var lineSpacing: CGFloat = 3
    /// Space between blocks, and between list items.
    var blockSpacing: CGFloat = 9
    var itemSpacing: CGFloat = 4
    /// The floating bubble: code wraps instead of scrolling sideways.
    var compact: Bool = true

    var body: Font { Fonts.prose(size: size) }
    var title: Font {
        Fonts.prose(size: size >= 14 ? 20 : size + 5.5, weight: Fonts.OpticalWeight.bold)
    }
    var heading: Font {
        Fonts.prose(size: size >= 14 ? 16.5 : size + 2.5, weight: Fonts.OpticalWeight.semibold)
    }
    var section: Font {
        Fonts.prose(size: size >= 14 ? 14.5 : size + 1, weight: Fonts.OpticalWeight.semibold)
    }
    var minor: Font { Fonts.prose(size: size, weight: Fonts.OpticalWeight.medium) }
    var marker: Font {
        Fonts.sans(size: size - 0.5, weight: Fonts.OpticalWeight.regular, tabularDigits: true)
    }
    var table: Font {
        Fonts.sans(size: size - 1.5, weight: Fonts.OpticalWeight.regular, tabularDigits: true)
    }
    var codeSize: CGFloat { size >= 14 ? 12.5 : (size * 0.88).rounded() }
    var code: Font { Fonts.mono(size: codeSize) }
    var codeLineSpacing: CGFloat { compact ? 2 : 2.5 }
    var meta: Font { Paper.Typography.ledgerMeta }

    func tracking(for headingLevel: Int) -> CGFloat {
        switch headingLevel {
        case 1: Paper.Typography.h1Tracking
        case 2: Paper.Typography.h2Tracking
        case 3: Paper.Typography.h3Tracking
        default: 0
        }
    }
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
    static let codeHeaderHeight: CGFloat = 26
}

private struct Blocks: View {
    let blocks: [any Markup]
    let spacing: CGFloat
    var rich = false

    var body: some View {
        VStack(alignment: .leading, spacing: spacing) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                Block(markup: block, first: index == 0, rich: rich)
            }
        }
    }
}

private struct Block: View {
    let markup: any Markup
    let first: Bool
    var rich = false
    @Environment(\.replyType) private var type
    @Environment(\.markdownEquations) private var equations

    var body: some View {
        switch markup {
        case let heading as Heading:
            // More room above than below, so a heading belongs to the text it introduces.
            VStack(alignment: .leading, spacing: 5) {
                Prose(
                    text: inline(heading, code: type.code),
                    font: font(heading.level),
                    spacing: 1.5,
                    tracking: type.tracking(for: heading.level))
                    .foregroundStyle(heading.level >= 4 ? Paper.inkSecondary : Paper.inkPrimary)
                // Top-level headings carry a hairline, so a long reply reads in sections.
                if heading.level <= 2 {
                    Rectangle().fill(Paper.borderSubtle).frame(height: 0.5)
                }
            }
            .padding(.top, first ? 0 : heading.level <= 2 ? type.blockSpacing * 0.6 : 2)
        case let paragraph as Paragraph:
            if rich, paragraph.childCount == 1, let link = paragraph.child(at: 0) as? Markdown.Link,
               let url = SafePreviewURL(link.destination) {
                WebLinkCard(url: url.value, title: String(inline(link).characters))
            } else if rich, paragraph.childCount == 1, let image = paragraph.child(at: 0) as? Markdown.Image,
                      let url = SafePreviewURL(image.source) {
                WebImageCard(url: url.value, title: String(inline(image).characters))
            } else {
                paragraphText(inline(paragraph, code: type.code))
            }
        case let list as UnorderedList:
            ListBlock(items: Array(list.listItems), start: nil)
        case let list as OrderedList:
            ListBlock(items: Array(list.listItems), start: Int(list.startIndex))
        case let code as CodeBlock:
            CodeBlockView(code: code.code, language: code.language)
        case let quote as BlockQuote:
            HStack(alignment: .top, spacing: 10) {
                RoundedRectangle(cornerRadius: 1.5).fill(Paper.borderStrong).frame(width: 3)
                Blocks(blocks: Array(quote.children), spacing: type.itemSpacing + 2)
                    .foregroundStyle(Paper.inkSecondary)
                    .padding(.vertical, 1)
            }
            .fixedSize(horizontal: false, vertical: true)
        case let table as Markdown.Table:
            TableView(table: table)
        case is ThematicBreak:
            Rectangle().fill(Paper.borderSubtle).frame(height: 0.5).padding(.vertical, 4)
        default:
            // HTML and anything newer than this renderer: show the source rather than drop it.
            Prose(text: AttributedString(markup.format()))
        }
    }

    @ViewBuilder
    private func paragraphText(_ text: AttributedString) -> some View {
        let parts = MarkdownMath.paragraphParts(text, equations: equations)
        if parts.count == 1, case .prose = parts[0] {
            Prose(text: text)
        } else {
            VStack(alignment: .leading, spacing: type.blockSpacing) {
                ForEach(Array(parts.enumerated()), id: \.offset) { _, part in
                    switch part {
                    case .prose(let content):
                        Prose(text: content)
                    case .equation(let equation):
                        let formula = MathTypesetter.text(equation, size: type.size + 2, display: true)
                        ViewThatFits(in: .horizontal) {
                            formula.fixedSize()
                            ScrollView(.horizontal) { formula.fixedSize() }
                        }
                        .frame(maxWidth: .infinity)
                    }
                }
            }
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

/// Text in the reply typeface, wrapped and selectable, with inline code and citations drawn as chips.
private struct Prose: View {
    let text: AttributedString
    var font: Font?
    var spacing: CGFloat?
    var tracking: CGFloat = 0
    @Environment(\.replyType) private var type
    @Environment(\.markdownEquations) private var equations

    var body: some View {
        chipped(text, equations: equations, size: type.size)
            .font(font ?? type.body)
            .tracking(tracking)
            .lineSpacing(spacing ?? type.lineSpacing)
            .textRenderer(InlinePillRenderer(
                codeFill: Paper.codeChipFill,
                codeBorder: Paper.codeChipBorder,
                citationFill: Paper.citationFill,
                citationBorder: Paper.citationBorder))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private enum ChipMetrics {
    static let gap: CGFloat = 3
    static let punctuationGap: CGFloat = 2
}

/// Marks a run that is inline code, so `InlinePillRenderer` can draw its chip.
private enum CodeRun: AttributedStringKey {
    typealias Value = Bool
    static let name = "bud.codeRun"
}

/// Marks a run that is a clickable source citation pill.
private enum CitationRun: AttributedStringKey {
    typealias Value = Bool
    static let name = "bud.citationRun"
}

/// Marks a pill run whose trailing character abuts punctuation (`.,:;!?)`).
private enum TightTrailingRun: AttributedStringKey {
    typealias Value = Bool
    static let name = "bud.tightTrailingRun"
}

private struct CodeChipAttribute: TextAttribute {}
private struct CitationPillAttribute: TextAttribute {}
private struct TightTrailingAttribute: TextAttribute {}

/// One Text from an attributed string, with inline code and citations tagged for pills.
@MainActor
private func chipped(
    _ text: AttributedString, equations: [Character: MarkdownEquation], size: CGFloat
) -> SwiftUI.Text {
    var out = SwiftUI.Text(verbatim: "")
    for run in text.runs {
        let content = text[run.range]
        var piece = SwiftUI.Text(AttributedString(content))
        if !equations.isEmpty, content.characters.contains(where: { equations[$0] != nil }) {
            piece = SwiftUI.Text(verbatim: "")
            var start = run.range.lowerBound
            for index in content.characters.indices {
                guard let equation = equations[content.characters[index]] else { continue }
                piece = piece + SwiftUI.Text(AttributedString(text[start..<index]))
                    + MathTypesetter.text(equation, size: size)
                start = content.characters.index(after: index)
            }
            piece = piece + SwiftUI.Text(AttributedString(text[start..<run.range.upperBound]))
        }
        if run[TightTrailingRun.self] == true {
            piece = piece.customAttribute(TightTrailingAttribute())
        }
        if run[CitationRun.self] == true {
            out = out + piece.customAttribute(CitationPillAttribute())
        } else if run[CodeRun.self] == true {
            out = out + piece.customAttribute(CodeChipAttribute())
        } else {
            out = out + piece
        }
    }
    return out
}

/// Draws rounded micro-pills behind inline code and clickable source citations.
private struct InlinePillRenderer: TextRenderer {
    let codeFill: Color
    let codeBorder: Color
    let citationFill: Color
    let citationBorder: Color

    func draw(layout: SwiftUI.Text.Layout, in context: inout GraphicsContext) {
        for line in layout {
            drawPills(in: line, context: &context, isCitation: false, fill: codeFill, border: codeBorder)
            drawPills(
                in: line, context: &context, isCitation: true,
                fill: citationFill, border: citationBorder)
            for run in line { context.draw(run) }
        }
    }

    private func drawPills(
        in line: SwiftUI.Text.Layout.Line,
        context: inout GraphicsContext,
        isCitation: Bool,
        fill: Color,
        border: Color?
    ) {
        var pills: [(rect: CGRect, tightRight: Bool)] = []
        var current: CGRect?
        var tightRight = false
        for run in line {
            let matches = isCitation
                ? run[CitationPillAttribute.self] != nil
                : run[CodeChipAttribute.self] != nil
            if matches {
                let bounds = run.typographicBounds.rect
                current = current.map { $0.union(bounds) } ?? bounds
                tightRight = run[TightTrailingAttribute.self] != nil
            } else if let done = current {
                pills.append((done, tightRight))
                current = nil
                tightRight = false
            }
        }
        if let done = current { pills.append((done, tightRight)) }
        for item in pills {
            let trailingGap = item.tightRight ? ChipMetrics.punctuationGap : ChipMetrics.gap
            var pill = item.rect
            // The stretch's bounds include the kerning after its last character.
            pill.size.width = max(0, pill.size.width - trailingGap)
            let padded = CGRect(
                x: pill.minX - ChipMetrics.gap,
                y: pill.minY - 1,
                width: pill.width + ChipMetrics.gap + trailingGap,
                height: pill.height + 2)
            let path = RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous)
                .path(in: padded)
            context.fill(path, with: .color(fill))
            if let border {
                context.stroke(path, with: .color(border), lineWidth: 0.5)
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
    /// line and the items stay aligned.
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

/// A code block on recessed paper with a hairline edge and a persistent quiet 26pt header bar.
/// In the window, long lines scroll sideways so code keeps its shape; in the bubble they wrap.
private struct CodeBlockView: View {
    let code: String
    let language: String?
    @Environment(\.replyType) private var type
    @StateObject private var copyHovered = Flag()
    @StateObject private var copied = Flag()

    private var source: String { code.hasSuffix("\n") ? String(code.dropLast()) : code }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            headerBar
            Rectangle().fill(Paper.borderSubtle).frame(height: 0.5)
            content
                .padding(.horizontal, 10)
                .padding(.vertical, 9)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Paper.recessed, in: shape)
        .clipShape(shape)
        .overlay(shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5))
    }

    private var shape: RoundedRectangle {
        RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
    }

    private var headerBar: some View {
        HStack(spacing: 8) {
            if let language, !language.isEmpty {
                Text(language.lowercased())
                    .font(type.meta)
                    .foregroundStyle(Paper.inkMuted)
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(source, forType: .string)
                copied.on = true
                Task { @MainActor in
                    try? await Task.sleep(for: .seconds(1.2))
                    copied.on = false
                }
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: copied.on ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 10, weight: .medium))
                    if copied.on {
                        Text("Copied")
                            .font(type.meta)
                    }
                }
                .foregroundStyle(copied.on || copyHovered.on ? Paper.inkPrimary : Paper.inkMuted)
                .padding(.horizontal, 6)
                .frame(height: 18)
                .background(
                    copyHovered.on ? Paper.fillHover : .clear,
                    in: RoundedRectangle(cornerRadius: Paper.radiusXS, style: .continuous))
            }
            .buttonStyle(.plain)
            .pointerStyle(.link)
            .onHover { copyHovered.on = $0 }
            .help(copied.on ? "Copied" : "Copy code")
            .accessibilityLabel("Copy code")
        }
        .padding(.horizontal, 10)
        .frame(height: Metrics.codeHeaderHeight)
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
}

/// A table in a hairline frame: a recessed header row, row dividers, padded cells, and the
/// column alignment the markdown asked for. Numbers use tabular digits, so columns line up.
private struct TableView: View {
    let table: Markdown.Table
    @Environment(\.replyType) private var type
    @Environment(\.markdownEquations) private var equations

    var body: some View {
        ViewThatFits(in: .horizontal) {
            grid.fixedSize(horizontal: true, vertical: true)
            ScrollView(.horizontal) { grid.fixedSize(horizontal: true, vertical: true) }
        }
    }

    private var grid: some View {
        Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
            GridRow { cells(Array(table.head.children), header: true) }
                .background(Paper.recessed)
            ForEach(Array(table.body.children.enumerated()), id: \.offset) { _, row in
                Rectangle().fill(Paper.borderSubtle).frame(height: 0.5).gridCellUnsizedAxes(.horizontal)
                GridRow { cells(Array(row.children), header: false) }
            }
        }
        .font(type.table)
        .clipShape(RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
                .strokeBorder(Paper.borderSubtle, lineWidth: 0.5))
    }

    @ViewBuilder
    private func cells(_ cells: [any Markup], header: Bool) -> some View {
        ForEach(Array(cells.enumerated()), id: \.offset) { index, cell in
            let alignment = alignment(index)
            chipped(
                inline(cell, code: Fonts.mono(size: max(10, type.codeSize - 1))),
                equations: equations, size: type.size - 1.5)
                .textRenderer(InlinePillRenderer(
                    codeFill: Paper.codeChipFill,
                    codeBorder: Paper.codeChipBorder,
                    citationFill: Paper.citationFill,
                    citationBorder: Paper.citationBorder))
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
                    if index > 0 { Rectangle().fill(Paper.borderSubtle).frame(width: 0.5) }
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
    let children = Array(markup.children)
    for (index, child) in children.enumerated() {
        let nextStartsWithPunctuation: Bool = {
            guard index + 1 < children.count, let nextText = children[index + 1] as? Markdown.Text,
                  let first = nextText.string.first
            else { return false }
            return ".,:;!?)]".contains(first)
        }()
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
            if nextStartsWithPunctuation { run[TightTrailingRun.self] = true }
            if let lastChar = out.characters.last, !lastChar.isWhitespace,
               let last = out.characters.indices.last
            {
                out[last..<out.endIndex].kern = ChipMetrics.gap
            }
            if let last = run.characters.indices.last {
                run[last..<run.endIndex].kern = nextStartsWithPunctuation
                    ? ChipMetrics.punctuationGap
                    : ChipMetrics.gap
            }
            out += run
        case let link as Markdown.Link:
            var run = inline(link, intent: intent, code: codeFont)
            if let destination = link.destination, let url = URL(string: destination) {
                run.link = url
                if url.scheme == Citations.scheme {
                    // Citations render as high-contrast 11pt mono micro-pills.
                    run.font = Paper.Typography.ledgerMeta
                    run.foregroundColor = Paper.citationInk
                    run[CitationRun.self] = true
                    if nextStartsWithPunctuation { run[TightTrailingRun.self] = true }
                    if let lastChar = out.characters.last, !lastChar.isWhitespace,
                       let last = out.characters.indices.last
                    {
                        out[last..<out.endIndex].kern = ChipMetrics.gap
                    }
                    if let last = run.characters.indices.last {
                        run[last..<run.endIndex].kern = nextStartsWithPunctuation
                            ? ChipMetrics.punctuationGap
                            : ChipMetrics.gap
                    }
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
