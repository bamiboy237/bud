import AppKit
import Markdown
import SwiftMath
import SwiftUI

struct MarkdownEquation: Equatable {
    let source: String
    let latex: String
    let display: Bool
}

struct MarkdownMath {
    let source: String
    let equations: [Character: MarkdownEquation]

    private static let delimiters = try! NSRegularExpression(
        pattern: #"(?<!\\)(?:\$\$(.+?)(?<!\\)\$\$|\\\[(.+?)(?<!\\)\\\]|\\\(([^\r\n]+?)(?<!\\)\\\)|(?<!\$)\$(?![\s$])((?:\\[^\r\n]|[^$\\\r\n])+?)(?<!\s)\$(?![\d$]))"#,
        options: .dotMatchesLineSeparators
    )

    init(_ input: String, document: Markdown.Document) {
        var equations: [Character: MarkdownEquation] = [:]
        let bytes = input.utf8
        var lineStarts = [bytes.startIndex]
        for index in bytes.indices where bytes[index] == 10 {
            lineStarts.append(bytes.index(after: index))
        }
        let protectedRanges = Self.literalRanges(document).compactMap { range -> Range<String.Index>? in
            func index(_ location: SourceLocation) -> String.Index? {
                guard lineStarts.indices.contains(location.line - 1) else { return nil }
                return bytes.index(
                    lineStarts[location.line - 1], offsetBy: location.column - 1, limitedBy: bytes.endIndex)
            }
            guard let start = index(range.lowerBound), let end = index(range.upperBound) else { return nil }
            return start..<end
        }.sorted { $0.lowerBound < $1.lowerBound }
        var searchStart = input.startIndex
        var matches: [NSTextCheckingResult] = []
        for protected in protectedRanges {
            if searchStart < protected.lowerBound {
                matches += Self.delimiters.matches(
                    in: input, options: .withTransparentBounds,
                    range: NSRange(searchStart..<protected.lowerBound, in: input))
            }
            searchStart = max(searchStart, protected.upperBound)
        }
        matches += Self.delimiters.matches(
            in: input, options: .withTransparentBounds, range: NSRange(searchStart..., in: input))
        var cursor = input.startIndex
        var rewritten = ""
        var markerValue: UInt32 = 0xF0000
        for match in matches {
            guard let range = Range(match.range, in: input),
                  let group = (1...4).first(where: { match.range(at: $0).location != NSNotFound }),
                  let inner = Range(match.range(at: group), in: input) else { continue }
            let latex = input[inner].trimmingCharacters(in: .whitespacesAndNewlines)
            guard !latex.isEmpty, latex.utf8.count <= 4096 else { continue }
            while markerValue < 0xFFFFE {
                let marker = Character(Unicode.Scalar(markerValue)!)
                markerValue += 1
                if input.contains(marker) { continue }
                rewritten += input[cursor..<range.lowerBound]
                rewritten.append(marker)
                equations[marker] = MarkdownEquation(source: String(input[range]), latex: latex, display: group <= 2)
                cursor = range.upperBound
                break
            }
        }
        self.source = rewritten + input[cursor...]
        self.equations = equations
    }

    enum ParagraphPart: Equatable {
        case prose(AttributedString)
        case equation(MarkdownEquation)
    }

    static func paragraphParts(
        _ text: AttributedString, equations: [Character: MarkdownEquation]
    ) -> [ParagraphPart] {
        guard equations.values.contains(where: \.display) else { return [.prose(text)] }
        let chars = text.characters
        var start = chars.startIndex
        var parts: [ParagraphPart] = []
        for line in chars.split(separator: "\n", omittingEmptySubsequences: false) {
            let content = String(line).trimmingCharacters(in: .whitespaces)
            guard content.count == 1, let marker = content.first,
                  let equation = equations[marker], equation.display else { continue }
            if start < line.startIndex {
                let end = chars.index(before: line.startIndex)
                parts.append(.prose(AttributedString(text[start..<end])))
            }
            parts.append(.equation(equation))
            start = line.endIndex == chars.endIndex ? chars.endIndex : chars.index(after: line.endIndex)
        }
        if parts.isEmpty || start < chars.endIndex {
            parts.append(.prose(AttributedString(text[start...])))
        }
        return parts
    }

    private static func literalRanges(_ markup: any Markup) -> [SourceRange] {
        switch markup {
        case is CodeBlock, is InlineCode, is HTMLBlock, is InlineHTML, is Markdown.Link, is Markdown.Image:
            return markup.range.map { [$0] } ?? []
        default:
            return markup.children.flatMap(literalRanges)
        }
    }
}

@MainActor
enum MathTypesetter {
    struct Glyph {
        let image: NSImage
        let descent: CGFloat
    }

    private final class Cached {
        let glyph: Glyph?
        init(_ glyph: Glyph?) { self.glyph = glyph }
    }

    private static let cache: NSCache<NSString, Cached> = {
        let cache = NSCache<NSString, Cached>()
        cache.countLimit = 256
        return cache
    }()

    static func glyph(_ equation: MarkdownEquation, size: CGFloat, display: Bool = false) -> Glyph? {
        let key = "\(size):\(display):\(equation.latex)" as NSString
        if let cached = cache.object(forKey: key) { return cached.glyph }
        var renderer = MathImage(
            latex: equation.latex, fontSize: size, textColor: .black,
            labelMode: display ? .display : .text)
        // Keep shallow glyphs above the drawing area's baseline cushion.
        renderer.contentInsets.bottom = size / 4
        let (_, image, layout) = renderer.asImage()
        let glyph: Glyph?
        if let image, let layout, image.size.width > 0, image.size.height > 0 {
            let height = layout.ascent + layout.descent
            let descent = layout.descent + renderer.contentInsets.bottom
                + (image.size.height - renderer.contentInsets.bottom - max(height, size / 2)) / 2
            image.isTemplate = true
            glyph = Glyph(image: image, descent: descent)
        } else {
            glyph = nil
        }
        cache.setObject(Cached(glyph), forKey: key)
        return glyph
    }

    static func text(_ equation: MarkdownEquation, size: CGFloat, display: Bool = false) -> SwiftUI.Text {
        guard let glyph = glyph(equation, size: size, display: display) else {
            return Text(verbatim: equation.source)
        }
        return Text(Image(nsImage: glyph.image).renderingMode(.template))
            .accessibilityLabel(equation.latex)
            .baselineOffset(-glyph.descent)
    }
}
