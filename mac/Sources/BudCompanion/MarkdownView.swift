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
    var selection: ReplyTextSelection?
    var query: String
    var activeRange: NSRange?
    var onQuote: ((String) -> Void)?
    @Environment(\.replyType) private var type
    @Bindable private var appearance = AppearanceStore.shared
    @StateObject private var tableReader = TableReaderSelection()

    init(
        _ text: String, rich: Bool = false, selection: ReplyTextSelection? = nil,
        query: String = "", activeRange: NSRange? = nil, onQuote: ((String) -> Void)? = nil
    ) {
        self.rich = rich
        self.selection = selection
        self.query = query
        self.activeRange = activeRange
        self.onQuote = onQuote
        parsed = ParsedMarkdown.parse(text)
    }

    static func selectableText(_ text: String) -> String {
        ParsedMarkdown.parse(text).selectable.string
    }

    @ViewBuilder
    var body: some View {
        if let selection, let onQuote {
            let document = parsed.selectable
            let expandsTables = rich && document.hasTables
            VStack(alignment: .leading, spacing: type.blockSpacing) {
                SelectableReplyView(
                    document: document, type: type, appearanceRevision: appearance.revision,
                    query: query, activeRange: activeRange, selection: selection, quote: onQuote,
                    expandTable: expandsTables ? { tableReader.table = document.table(at: $0) } : nil
                )
                // Place the expand controls in the conversation's existing trailing margin.
                .padding(.trailing, expandsTables ? -TableExpandButton.size : 0)
                if rich {
                    ForEach(Array(parsed.blocks.enumerated()), id: \.offset) { _, block in
                        if let paragraph = block as? Paragraph, paragraph.childCount == 1 {
                            if let link = paragraph.child(at: 0) as? Markdown.Link,
                               let url = SafePreviewURL(link.destination) {
                                WebLinkCard(url: url.value, title: String(inline(link).characters))
                            } else if let image = paragraph.child(at: 0) as? Markdown.Image,
                                      let url = SafePreviewURL(image.source) {
                                WebImageCard(url: url.value, title: String(inline(image).characters))
                            }
                        }
                    }
                }
            }
            .sheet(isPresented: Binding(
                get: { tableReader.table != nil },
                set: { if !$0 { tableReader.table = nil } }
            )) {
                if let table = tableReader.table {
                    TableReader(table: table)
                        .environment(\.markdownEquations, parsed.equations)
                }
            }
        } else {
            Blocks(blocks: parsed.blocks, spacing: type.blockSpacing, rich: rich)
                .environment(\.markdownEquations, parsed.equations)
                .tint(Paper.accent)
        }
    }
}

private final class TableReaderSelection: ObservableObject {
    @Published var table: Markdown.Table?
}

@MainActor
private enum ParsedMarkdown {
    @MainActor
    final class Entry {
        let blocks: [any Markup]
        let equations: [Character: MarkdownEquation]
        lazy var selectable = SelectableMarkdown(blocks: blocks, equations: equations)

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

@MainActor
private final class SelectableMarkdown {
    private enum Style {
        case prose, heading(Int), code(String?), table(Markdown.Table)
    }

    private struct BlockText {
        let text: AttributedString
        let style: Style
        let indent: Int
        let separator: String
    }

    private let blocks: [BlockText]
    private let equations: [Character: MarkdownEquation]
    let string: String
    let hasTables: Bool

    init(blocks: [any Markup], equations: [Character: MarkdownEquation]) {
        self.equations = equations
        self.blocks = Self.flatten(blocks, equations: equations, indent: 0, separator: "\n\n")
        string = self.blocks.enumerated().map { index, block in
            (index == 0 ? "" : block.separator) + String(block.text.characters)
        }.joined()
        hasTables = self.blocks.contains {
            if case .table = $0.style { return true }
            return false
        }
    }

    func table(at index: Int) -> Markdown.Table? {
        if case .table(let table) = blocks[index].style { return table }
        return nil
    }

    private static func flatten(
        _ blocks: [any Markup], equations: [Character: MarkdownEquation], indent: Int, separator: String
    ) -> [BlockText] {
        blocks.flatMap { block -> [BlockText] in
            switch block {
            case let quote as BlockQuote:
                return flatten(Array(quote.children), equations: equations, indent: indent + 1, separator: separator)
            case let list as OrderedList:
                return listText(Array(list.listItems), start: Int(list.startIndex), equations: equations, indent: indent)
            case let list as UnorderedList:
                return listText(Array(list.listItems), start: nil, equations: equations, indent: indent)
            case let code as CodeBlock:
                let source = code.code.hasSuffix("\n") ? String(code.code.dropLast()) : code.code
                return [.init(text: AttributedString(source), style: .code(code.language), indent: indent, separator: separator)]
            case let heading as Heading:
                return [.init(text: inlineText(heading, equations: equations), style: .heading(heading.level),
                              indent: indent, separator: separator)]
            case let table as Markdown.Table:
                let rows = [table.head.children] + table.body.children.map(\.children)
                let text = rows.map { row in
                    row.map { String(inlineText($0, equations: equations).characters) }.joined(separator: "   |   ")
                }.joined(separator: "\n")
                return [.init(text: AttributedString(text), style: .table(table), indent: indent, separator: separator)]
            case is ThematicBreak:
                return [.init(text: AttributedString("────────"), style: .prose, indent: indent, separator: separator)]
            case is Paragraph:
                return [.init(text: inlineText(block, equations: equations), style: .prose,
                              indent: indent, separator: separator)]
            default:
                return [.init(text: AttributedString(block.format()), style: .prose, indent: indent, separator: separator)]
            }
        }
    }

    private static func listText(
        _ items: [ListItem], start: Int?, equations: [Character: MarkdownEquation], indent: Int
    ) -> [BlockText] {
        items.enumerated().flatMap { index, item -> [BlockText] in
            var blocks = flatten(Array(item.children), equations: equations, indent: indent + 1, separator: "\n")
            guard let first = blocks.first else { return [] }
            let marker: String
            if let checkbox = item.checkbox {
                marker = checkbox == .checked ? "☑ " : "☐ "
            } else if let start {
                marker = "\(start + index). "
            } else {
                marker = "• "
            }
            blocks[0] = .init(text: AttributedString(marker) + first.text, style: first.style,
                              indent: first.indent, separator: index == 0 ? "\n\n" : "\n")
            return blocks
        }
    }

    private static func inlineText(
        _ markup: any Markup, equations: [Character: MarkdownEquation]
    ) -> AttributedString {
        let text = inline(markup)
        guard !equations.isEmpty else { return text }
        var result = AttributedString()
        var start = text.startIndex
        for index in text.characters.indices {
            let marker = text.characters[index]
            guard let equation = equations[marker] else { continue }
            result += AttributedString(text[start..<index])
            var formula = AttributedString(equation.source)
            formula[EquationRun.self] = marker
            result += formula
            start = text.characters.index(after: index)
        }
        result += AttributedString(text[start...])
        return result
    }

    func attributed(type: ReplyType, expandsTables: Bool) -> NSAttributedString {
        let output = NSMutableAttributedString(string: "")
        for (index, block) in blocks.enumerated() {
            let paragraph = NSMutableParagraphStyle()
            paragraph.lineSpacing = type.lineSpacing
            paragraph.firstLineHeadIndent = CGFloat(block.indent) * 14
            paragraph.headIndent = paragraph.firstLineHeadIndent
            var size = type.scaledSize
            var weight = Fonts.OpticalWeight.regular
            var content = block.text
            var mono = false
            var codeText: String?
            switch block.style {
            case .prose:
                break
            case .heading(let level):
                size += level == 1 ? 6 : level == 2 ? 2.5 : level == 3 ? 0.5 : 0
                weight = level == 1 ? Fonts.OpticalWeight.bold : Fonts.OpticalWeight.semibold
                paragraph.lineSpacing = 1.5
            case .code(let language):
                let source = String(content.characters)
                codeText = source
                content = Syntax.highlight(source, language: language)
                mono = true
                size = type.codeSize
                paragraph.lineSpacing = type.codeLineSpacing
            case .table:
                mono = true
                size = type.codeSize
            }
            let bodyFont = Self.proseFont(size: size, weight: weight)
            if index > 0 {
                output.append(NSAttributedString(
                    string: block.separator, attributes: [.font: bodyFont, .paragraphStyle: paragraph]))
            }
            let start = output.length
            for run in content.runs {
                let intent = run.inlinePresentationIntent ?? []
                let isCode = run[CodeRun.self] == true
                var font = mono || isCode || run[CitationRun.self] == true
                    ? Fonts.monoNSFont(size: isCode ? type.codeSize : size)
                    : (intent.contains(.stronglyEmphasized)
                        ? Self.proseFont(size: size, weight: Fonts.OpticalWeight.bold) : bodyFont)
                if intent.contains(.emphasized) { font = NSFontManager.shared.convert(font, toHaveTrait: .italicFontMask) }
                var attributes: [NSAttributedString.Key: Any] = [
                    .font: font, .paragraphStyle: paragraph,
                    .foregroundColor: NSColor(run.foregroundColor ?? Paper.inkPrimary),
                ]
                if isCode || codeText != nil { attributes[.backgroundColor] = NSColor(Paper.codeChipFill) }
                if let codeText { attributes[.replyCode] = codeText }
                if let marker = run[EquationRun.self] { attributes[.replyEquation] = String(marker) }
                if let link = run.link {
                    attributes[.link] = link
                    attributes[.foregroundColor] = NSColor(Paper.accent)
                    attributes[.underlineStyle] = NSUnderlineStyle.single.rawValue
                }
                if intent.contains(.strikethrough) { attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue }
                output.append(NSAttributedString(string: String(content[run.range].characters), attributes: attributes))
            }
            if expandsTables, case .table = block.style, output.length > start {
                output.addAttribute(.replyTable, value: index, range: NSRange(
                    location: start, length: output.length - start))
            }
        }
        return output
    }

    func renderEquations(in text: NSMutableAttributedString, type: ReplyType) {
        guard !equations.isEmpty else { return }
        var formulas: [(MarkdownEquation, NSRange)] = []
        text.enumerateAttribute(.replyEquation, in: NSRange(location: 0, length: text.length)) { marker, range, _ in
            if let marker = (marker as? String)?.first, let equation = equations[marker] {
                formulas.append((equation, range))
            }
        }
        let ink = NSColor(Paper.inkPrimary)
        for (equation, range) in formulas.reversed() {
            let size = equation.display ? type.scaledSize + 2 : type.scaledSize
            guard let glyph = MathTypesetter.glyph(equation, size: size, display: equation.display) else { continue }
            let image = NSImage(size: glyph.image.size, flipped: false) { bounds in
                guard let context = NSGraphicsContext.current?.cgContext else { return false }
                context.beginTransparencyLayer(auxiliaryInfo: nil)
                glyph.image.draw(in: bounds)
                ink.setFill()
                bounds.fill(using: .sourceAtop)
                context.endTransparencyLayer()
                return true
            }
            let attachment = NSTextAttachment()
            attachment.attachmentCell = NSTextAttachmentCell(imageCell: image)
            let formula = NSMutableAttributedString(attachment: attachment)
            let formulaRange = NSRange(location: 0, length: 1)
            var attributes = text.attributes(at: range.location, effectiveRange: nil)
            attributes[.replyEquation] = equation
            attributes[.baselineOffset] = -glyph.descent
            text.enumerateAttribute(.backgroundColor, in: range) { color, _, stop in
                if let color {
                    attributes[.backgroundColor] = color
                    stop.pointee = true
                }
            }
            formula.addAttributes(attributes, range: formulaRange)
            text.replaceCharacters(in: range, with: formula)
        }
    }

    private static func proseFont(size: CGFloat, weight: CGFloat) -> NSFont {
        let font = Fonts.sansNSFont(size: size, weight: weight)
        guard AppearanceStore.activeFont == .editorial else { return font }
        return font.fontDescriptor.withDesign(.serif).flatMap { NSFont(descriptor: $0, size: size) } ?? font
    }
}

private extension NSAttributedString.Key {
    static let replyCode = Self("bud.replyCode")
    static let replyEquation = Self("bud.replyEquation")
    static let replyTable = Self("bud.replyTable")
}

private enum EquationRun: AttributedStringKey {
    typealias Value = Character
    static let name = "bud.replyEquation"
}

@MainActor
final class ReplyTextSelection: ObservableObject {
    fileprivate weak var view: ReplyTextView?
    var text: String? { view?.selectedText }
}

private struct SelectableReplyView: NSViewRepresentable {
    let document: SelectableMarkdown
    let type: ReplyType
    let appearanceRevision: Int
    let query: String
    let activeRange: NSRange?
    let selection: ReplyTextSelection
    let quote: (String) -> Void
    let expandTable: ((Int) -> Void)?
    @Environment(\.openURL) private var openURL

    func makeNSView(context: Context) -> ReplyTextView { ReplyTextView() }

    func updateNSView(_ view: ReplyTextView, context: Context) {
        selection.view = view
        view.onQuote = quote
        view.onLink = { openURL($0) }
        view.expandTable = expandTable
        view.update(document: document, type: type, appearanceRevision: appearanceRevision,
                    query: query, activeRange: activeRange)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: ReplyTextView, context: Context) -> CGSize? {
        guard let width = proposal.width, width.isFinite, width > 0 else { return nil }
        let gutter = expandTable == nil ? 0 : TableExpandButton.size
        return CGSize(width: width, height: nsView.height(fitting: max(1, width - gutter)))
    }
}

@MainActor
final class ReplyTextView: NSTextView, NSTextViewDelegate {
    var onQuote: ((String) -> Void)?
    var onLink: ((URL) -> Void)?
    fileprivate var expandTable: ((Int) -> Void)?
    private var contentKey: ContentKey?
    private var tables: [TableLocation] = []
    private var tableButtons: [Int: NSHostingView<TableExpandButton>] = [:]

    private struct TableLocation {
        let index: Int
        let start: Int
        let width: CGFloat
    }

    private struct ContentKey: Equatable {
        let document: ObjectIdentifier
        let type: ReplyType
        let appearance: Int
        let query: String
        let activeRange: NSRange?
        let expandsTables: Bool
    }

    init() {
        let storage = NSTextStorage()
        let layout = NSLayoutManager()
        let container = NSTextContainer(size: CGSize(width: 0, height: CGFloat.greatestFiniteMagnitude))
        storage.addLayoutManager(layout)
        layout.addTextContainer(container)
        super.init(frame: .zero, textContainer: container)
        delegate = self
        isEditable = false
        isSelectable = true
        isVerticallyResizable = false
        isHorizontallyResizable = false
        drawsBackground = false
        textContainerInset = .zero
        container.lineFragmentPadding = 0
        container.widthTracksTextView = false
        linkTextAttributes = [:]
        setAccessibilityLabel("Assistant reply")
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    fileprivate func update(
        document: SelectableMarkdown, type: ReplyType, appearanceRevision: Int, query: String, activeRange: NSRange?
    ) {
        let key = ContentKey(document: ObjectIdentifier(document), type: type, appearance: appearanceRevision,
                             query: query, activeRange: activeRange, expandsTables: expandTable != nil)
        guard contentKey != key, let textStorage else { return }
        let text = NSMutableAttributedString(attributedString: document.attributed(
            type: type, expandsTables: key.expandsTables))
        let matchColor = NSColor(Paper.fillSubtle)
        let activeColor = NSColor(Paper.accent.opacity(0.24))
        for range in ChatFindMatch.ranges(in: text.string, query: query) {
            text.addAttribute(.backgroundColor, value: range == activeRange ? activeColor : matchColor, range: range)
        }
        document.renderEquations(in: text, type: type)
        tables = []
        if key.expandsTables {
            text.enumerateAttribute(.replyTable, in: NSRange(location: 0, length: text.length)) { index, range, _ in
                guard let index = index as? Int,
                      let font = text.attribute(.font, at: range.location, effectiveRange: nil) as? NSFont,
                      let paragraph = text.attribute(.paragraphStyle, at: range.location, effectiveRange: nil)
                        as? NSParagraphStyle else { return }
                let lines = (text.string as NSString).substring(with: range)
                let width = MarkdownTableLayout.textWidth(lines, font: font) + paragraph.headIndent
                tables.append(TableLocation(index: index, start: range.location, width: width))
            }
        }
        let range = selectedRange()
        textStorage.setAttributedString(text)
        if NSMaxRange(range) <= text.length { setSelectedRange(range) }
        contentKey = key
        needsLayout = true
        invalidateIntrinsicContentSize()
    }

    func height(fitting width: CGFloat) -> CGFloat {
        guard let textContainer, let layoutManager else { return 0 }
        if textContainer.size.width != width {
            textContainer.size = CGSize(width: width, height: CGFloat.greatestFiniteMagnitude)
        }
        layoutManager.ensureLayout(for: textContainer)
        return ceil(layoutManager.usedRect(for: textContainer).maxY)
    }

    override func setFrameSize(_ newSize: NSSize) {
        let widthChanged = frame.width != newSize.width
        super.setFrameSize(newSize)
        if widthChanged, !tables.isEmpty { needsLayout = true }
    }

    override func layout() {
        super.layout()
        guard !tables.isEmpty || !tableButtons.isEmpty else { return }
        guard let layoutManager else { return }

        let gutter = expandTable == nil ? 0 : TableExpandButton.size
        let width = max(1, bounds.width - gutter)
        _ = height(fitting: width)
        let overflowing = tables.filter { MarkdownTableLayout.overflows($0.width, available: width) }
        for index in Set(tableButtons.keys).subtracting(overflowing.map(\.index)) {
            tableButtons.removeValue(forKey: index)?.removeFromSuperview()
        }
        for table in overflowing {
            let button: NSHostingView<TableExpandButton>
            if let existing = tableButtons[table.index] {
                button = existing
            } else {
                let index = table.index
                button = NSHostingView(rootView: TableExpandButton { [weak self] in self?.expandTable?(index) })
                tableButtons[index] = button
                addSubview(button)
            }
            let glyph = layoutManager.glyphIndexForCharacter(at: table.start)
            let line = layoutManager.lineFragmentRect(forGlyphAt: glyph, effectiveRange: nil)
            let size = TableExpandButton.size
            button.frame = CGRect(
                x: width, y: max(0, textContainerOrigin.y + line.midY - size / 2), width: size, height: size)
        }
    }

    var selectedText: String? {
        let range = selectedRange()
        guard let textStorage, range.length > 0, NSMaxRange(range) <= textStorage.length else { return nil }
        let text = textStorage.string as NSString
        var selection = ""
        textStorage.enumerateAttribute(.replyEquation, in: range) { equation, part, _ in
            if let equation = equation as? MarkdownEquation {
                selection += equation.source
            } else {
                selection += text.substring(with: part)
            }
        }
        return selection
    }

    @objc func quoteSelection(_ sender: Any?) {
        if let selectedText { onQuote?(selectedText) }
    }

    override func menu(for event: NSEvent) -> NSMenu? {
        let menu = super.menu(for: event) ?? NSMenu()
        if selectedText != nil {
            let item = menu.insertItem(withTitle: "Quote in Reply", action: #selector(quoteSelection(_:)),
                                       keyEquivalent: "", at: 0)
            item.target = self
        }
        let location = characterIndexForInsertion(at: convert(event.locationInWindow, from: nil))
        if let textStorage, location < textStorage.length {
            if let code = textStorage.attribute(.replyCode, at: location, effectiveRange: nil) as? String {
                addCopyItem("Copy code", text: code, menu: menu)
            }
            if let equation = textStorage.attribute(.replyEquation, at: location, effectiveRange: nil) as? MarkdownEquation {
                addCopyItem("Copy LaTeX", text: equation.latex, menu: menu)
            }
        }
        return menu
    }

    private func addCopyItem(_ title: String, text: String, menu: NSMenu) {
        let item = menu.addItem(withTitle: title, action: #selector(copyText(_:)), keyEquivalent: "")
        item.target = self
        item.representedObject = text
    }

    @objc private func copyText(_ sender: NSMenuItem) {
        guard let text = sender.representedObject as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        guard let url = link as? URL else { return false }
        onLink?(url)
        return true
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
struct ReplyType: Equatable {
    var size: CGFloat = 13
    var lineSpacing: CGFloat = 3
    /// Space between blocks, and between list items.
    var blockSpacing: CGFloat = 9
    var itemSpacing: CGFloat = 4
    /// The floating bubble: code wraps instead of scrolling sideways.
    var compact: Bool = true
    var zoom: CGFloat = 0

    var scaledSize: CGFloat { size + zoom }
    var body: Font { Fonts.prose(size: scaledSize) }
    var title: Font {
        Fonts.prose(size: (size >= 14 ? 20 : size + 5.5) + zoom, weight: Fonts.OpticalWeight.bold)
    }
    var heading: Font {
        Fonts.prose(size: (size >= 14 ? 16.5 : size + 2.5) + zoom, weight: Fonts.OpticalWeight.semibold)
    }
    var section: Font {
        Fonts.prose(size: (size >= 14 ? 14.5 : size + 1) + zoom, weight: Fonts.OpticalWeight.semibold)
    }
    var minor: Font { Fonts.prose(size: scaledSize, weight: Fonts.OpticalWeight.medium) }
    var marker: Font {
        Fonts.sans(size: scaledSize - 0.5, weight: Fonts.OpticalWeight.regular, tabularDigits: true)
    }
    var table: Font {
        Fonts.sans(size: scaledSize - 1.5, weight: Fonts.OpticalWeight.regular, tabularDigits: true)
    }
    var codeSize: CGFloat { (size >= 14 ? 12.5 : (size * 0.88).rounded()) + zoom }
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
                        DisplayMathView(equation: equation)
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

private struct DisplayMathView: View {
    let equation: MarkdownEquation
    @Environment(\.replyType) private var type
    @StateObject private var hover = Flag()

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
        let formula = MathTypesetter.text(equation, size: type.scaledSize + 2, display: true)
        ViewThatFits(in: .horizontal) {
            formula.fixedSize()
            ScrollView(.horizontal) { formula.fixedSize() }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 12) // Reserve room for the copy button on both sides to keep the equation centered.
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Paper.recessed, in: shape)
        .overlay { shape.strokeBorder(Paper.borderSubtle, lineWidth: 0.5).allowsHitTesting(false) }
        .overlay(alignment: .topTrailing) {
            MarkdownCopyButton(source: equation.latex, label: "Copy LaTeX", revealed: hover.on)
                .padding(6)
        }
        .contentShape(shape)
        .onHover { hover.on = $0 }
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
        chipped(text, equations: equations, size: type.scaledSize)
            .font(font ?? type.body)
            .tracking(tracking)
            .lineSpacing(spacing ?? type.lineSpacing)
            .textRenderer(InlinePillRenderer(
                codeFill: Paper.codeChipFill,
                codeBorder: Paper.codeChipBorder,
                citationFill: Paper.fillSubtle))
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

    func draw(layout: SwiftUI.Text.Layout, in context: inout GraphicsContext) {
        for line in layout {
            drawPills(in: line, context: &context, isCitation: false, fill: codeFill, border: codeBorder)
            drawPills(
                in: line, context: &context, isCitation: true,
                fill: citationFill, border: nil)
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
        return max(Metrics.marker, CGFloat(digits) * type.scaledSize * 0.58 + 5)
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
private struct CodeBlockView: View {
    let code: String
    let language: String?
    @Environment(\.replyType) private var type

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
            MarkdownCopyButton(source: source, label: "Copy code")
        }
        .padding(.horizontal, 10)
        .frame(height: Metrics.codeHeaderHeight)
    }

    private var content: some View {
        Text(Syntax.highlight(source, language: language))
            .font(type.code)
            .lineSpacing(type.codeLineSpacing)
            .foregroundStyle(Paper.inkPrimary)
            .fixedSize(horizontal: false, vertical: true)
            .textSelection(.enabled)
    }
}

private struct MarkdownCopyButton: View {
    let source: String
    let label: String
    var revealed = true
    @StateObject private var copied = Flag()
    @FocusState private var focused: Bool

    var body: some View {
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
                .font(.system(size: 11, weight: .medium))
                .opacity(revealed || focused || copied.on ? 1 : 0)
        }
        .buttonStyle(IconButton(size: 22, active: copied.on))
        .focused($focused)
        .help(copied.on ? "Copied" : label)
        .accessibilityLabel(copied.on ? "Copied" : label)
    }
}

enum MarkdownTableLayout {
    static func overflows(_ width: CGFloat, available: CGFloat) -> Bool {
        width > available + 1
    }

    static func textWidth(_ rows: String, font: NSFont) -> CGFloat {
        rows.split(separator: "\n").reduce(0) { widest, row in
            max(widest, (String(row) as NSString).size(withAttributes: [.font: font]).width)
        }
    }

    static func pinOffset(origin: CGPoint, header: Bool, firstColumn: Bool) -> CGSize {
        CGSize(width: firstColumn ? max(0, -origin.x) : 0, height: header ? max(0, -origin.y) : 0)
    }
}

private struct TableExpandButton: View {
    static let size: CGFloat = 24
    let expand: () -> Void

    var body: some View {
        Button(action: expand) {
            Image(systemName: "arrow.up.left.and.arrow.down.right")
                .font(.system(size: 10, weight: .medium))
        }
        .buttonStyle(IconButton(size: Self.size))
        .help("Expand table")
        .accessibilityLabel("Expand table")
    }
}

private struct TableView: View {
    private final class ScrollEdges: ObservableObject {
        @Published var hidden: Edge.Set = .trailing
    }

    let table: Markdown.Table
    @StateObject private var edges = ScrollEdges()
    @StateObject private var expanded = Flag()

    var body: some View {
        ViewThatFits(in: .horizontal) {
            TableGrid(table: table).fixedSize()
            VStack(alignment: .trailing, spacing: 2) {
                TableExpandButton { expanded.on = true }
                scrollableGrid
            }
        }
        .sheet(isPresented: $expanded.on) { TableReader(table: table) }
    }

    private var scrollableGrid: some View {
        ScrollView(.horizontal) {
            TableGrid(table: table).fixedSize()
        }
        .onScrollGeometryChange(for: Edge.Set.self) { geometry in
            var hidden: Edge.Set = []
            if geometry.visibleRect.minX > 1 { hidden.insert(.leading) }
            if MarkdownTableLayout.overflows(geometry.contentSize.width, available: geometry.visibleRect.maxX) {
                hidden.insert(.trailing)
            }
            return hidden
        } action: { _, hidden in
            edges.hidden = hidden
        }
        .mask {
            HStack(spacing: 0) {
                if edges.hidden.contains(.leading) {
                    LinearGradient(colors: [.clear, .black], startPoint: .leading, endPoint: .trailing)
                        .frame(width: 12)
                }
                Rectangle()
                if edges.hidden.contains(.trailing) {
                    LinearGradient(colors: [.black, .clear], startPoint: .leading, endPoint: .trailing)
                        .frame(width: 12)
                }
            }
        }
    }
}

private struct TableReader: View {
    let table: Markdown.Table
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Table")
                    .font(Paper.Typography.label)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark").font(.system(size: 10, weight: .medium))
                }
                .buttonStyle(IconButton(size: 24))
                .keyboardShortcut(.cancelAction)
                .help("Close table")
                .accessibilityLabel("Close table")
            }
            .padding(.horizontal, 14)
            .frame(height: 40)
            Rectangle().fill(Paper.hairline).frame(height: 0.5)
            ScrollView([.horizontal, .vertical]) {
                TableGrid(table: table, pinned: true).fixedSize()
            }
            .padding(14)
        }
        .frame(width: 760, height: 460)
        .foregroundStyle(Paper.inkPrimary)
        .background(Paper.canvas)
    }
}

/// A hairline grid with the column alignments from the Markdown source.
private struct TableGrid: View {
    let table: Markdown.Table
    var pinned = false
    @Environment(\.replyType) private var type
    @Environment(\.markdownEquations) private var equations

    var body: some View {
        let rows = Array(table.body.children)
        Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
            GridRow { cells(Array(table.head.children), header: true, divider: !rows.isEmpty) }
            ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                GridRow { cells(Array(row.children), header: false, divider: index < rows.count - 1) }
            }
        }
        .font(type.table)
        .clipShape(RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Paper.radiusSM, style: .continuous)
                .strokeBorder(Paper.borderSubtle, lineWidth: 0.5))
    }

    @ViewBuilder
    private func cells(_ cells: [any Markup], header: Bool, divider: Bool) -> some View {
        ForEach(Array(cells.enumerated()), id: \.offset) { index, cell in
            let alignment = alignment(index)
            let pinHeader = pinned && header
            let pinColumn = pinned && index == 0
            chipped(
                inline(cell, code: Fonts.mono(size: max(10, type.codeSize - 1))),
                equations: equations, size: type.scaledSize - 1.5)
                .textRenderer(InlinePillRenderer(
                    codeFill: Paper.codeChipFill,
                    codeBorder: Paper.codeChipBorder,
                    citationFill: Paper.fillSubtle))
                .fontWeight(header ? .semibold : .regular)
                .foregroundStyle(header ? Paper.inkSecondary : Paper.inkPrimary)
                .multilineTextAlignment(alignment.text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 200, alignment: alignment.frame)
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                // Fill the column, so dividers and the header tint line up on every row.
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: alignment.frame)
                .background(header ? Paper.recessed : pinColumn ? Paper.canvas : .clear)
                .overlay(alignment: .trailing) {
                    if index < cells.count - 1 { Rectangle().fill(Paper.borderSubtle).frame(width: 0.5) }
                }
                .overlay(alignment: .bottom) {
                    if divider { Rectangle().fill(Paper.borderSubtle).frame(height: 0.5) }
                }
                .gridColumnAlignment(alignment.horizontal)
                .visualEffect { content, geometry in
                    content.offset(MarkdownTableLayout.pinOffset(
                        origin: geometry.frame(in: .scrollView).origin,
                        header: pinHeader, firstColumn: pinColumn))
                }
                .zIndex((pinHeader ? 2 : 0) + (pinColumn ? 1 : 0))
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
                    // Citations use compact mono text inside their chips.
                    run.font = Paper.Typography.ledgerMeta
                    run.foregroundColor = Paper.inkSecondary
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
                    run.foregroundColor = Paper.accent
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
