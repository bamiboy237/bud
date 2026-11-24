import AppKit
import Markdown
import SwiftUI
import Testing
@testable import BudCompanion

struct MarkdownMathTests {
    @Test
    func inlineMathKeepsItsPlaceInTheSentence() {
        let math = parse(#"Use \(df=n-1=21\) and $$t_{0.975,21}$$ for this interval."#)
        #expect(Set(math.equations.values.map(\.latex)) == ["df=n-1=21", "t_{0.975,21}"])
        #expect(MarkdownMath.paragraphParts(AttributedString(math.source), equations: math.equations)
                == [.prose(AttributedString(math.source))])
    }

    @Test
    func onlyExplicitStandaloneDisplayMathBreaksTheParagraph() throws {
        let math = parse(#"""
        Start with $t$.
        \[\frac{x}{y}\]
        $$z^2$$
        The result is $r$.
        """#)
        let lines = math.source.split(separator: "\n")
        try #require(lines.count == 4)
        let fraction = try #require(math.equations[try #require(lines[1].first)])
        let power = try #require(math.equations[try #require(lines[2].first)])
        #expect(fraction.latex == #"\frac{x}{y}"#)
        #expect(power.latex == "z^2")
        #expect(MarkdownMath.paragraphParts(AttributedString(math.source), equations: math.equations)
                == [.prose(AttributedString(String(lines[0]))), .equation(fraction), .equation(power),
                    .prose(AttributedString(String(lines[3])))])

        let standaloneInline = parse("$t$")
        let text = AttributedString(standaloneInline.source)
        #expect(MarkdownMath.paragraphParts(text, equations: standaloneInline.equations) == [.prose(text)])
    }

    @Test
    func preservesCodeCurrencyAndLinks() {
        let source = #"""
        🐕 `$not_math$` then $x_1$; pay $5 or $10, not \$15.
        [Price $20](https://example.com/$file$) and \(y^2\).

        ```latex
        \[z^2\]
        ```

            $$literal$$
        """#
        let math = parse(source)
        #expect(Set(math.equations.values.map(\.latex)) == ["x_1", "y^2"])
        let restored = math.source.map { math.equations[$0]?.source ?? String($0) }.joined()
        #expect(restored == source)
    }

    @Test
    func unfinishedMathStaysLiteralUntilTheDelimiterCloses() {
        let incomplete = #"Then \(t_{0.975,21}"#
        #expect(parse(incomplete).source == incomplete)
        let complete = parse(incomplete + #"\)"#)
        #expect(complete.equations.values.first?.latex == "t_{0.975,21}")
    }

    @MainActor
    @Test
    func nativeMathRendersInTheReplyAndInvalidMathFallsBack() throws {
        let formula = MarkdownEquation(source: #"$t_{0.975,21}$"#, latex: "t_{0.975,21}", display: false)
        let glyph = try #require(MathTypesetter.glyph(formula, size: 14))
        #expect(glyph.image.size.width > 0)
        let invalid = MarkdownEquation(source: #"$\budunknown$"#, latex: #"\budunknown"#, display: false)
        #expect(MathTypesetter.glyph(invalid, size: 14) == nil)

        for scheme in [ColorScheme.light, .dark] {
            let reply = MarkdownView(#"""
                For a 95% confidence interval, use \(df=n-1=21\) and \(t_{0.975,21}=2.080\).
                The first formula stays in this sentence.

                \[\frac{\bar{x}-\mu}{s/\sqrt{n}}\]

                In code: `\(df=n-1\)`. A price like $5 or $10 stays plain.
                """#)
                .padding(16)
                .frame(width: 322, alignment: .leading)
                .foregroundStyle(Paper.inkPrimary)
                .background(Paper.canvas)
                .environment(\.colorScheme, scheme)
            let renderer = ImageRenderer(content: reply)
            renderer.scale = 2
            let rendered = try #require(renderer.cgImage)
            #expect(rendered.width == 644)
            #expect(rendered.height > 100)
            if let directory = ProcessInfo.processInfo.environment["BUD_MARKDOWN_SNAPSHOTS"] {
                let bitmap = NSBitmapImageRep(cgImage: rendered)
                let png = try #require(bitmap.representation(using: .png, properties: [:]))
                try png.write(to: URL(filePath: directory).appending(path: "math-\(scheme).png"))
            }
        }
    }

    private func parse(_ text: String) -> MarkdownMath {
        MarkdownMath(text, document: Markdown.Document(parsing: text))
    }
}
