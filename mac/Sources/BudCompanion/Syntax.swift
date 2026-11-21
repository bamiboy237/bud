import SwiftUI

/// Light syntax colour for code blocks, in the One Light and One Dark palettes Zed uses:
/// keywords, strings, numbers, comments, function calls, and type names. A small scanner, not
/// a parser: it reads tokens left to right, so an odd construct only loses its colour.
enum Syntax {
    enum Kind: Equatable {
        case plain, keyword, string, number, comment, function, type
    }

    static func color(_ kind: Kind) -> Color? {
        switch kind {
        case .plain: nil
        case .keyword: Paper.dynamic(light: 0xA626A4, dark: 0xC678DD)
        case .string: Paper.dynamic(light: 0x50A14F, dark: 0x98C379)
        case .number: Paper.dynamic(light: 0x986801, dark: 0xD19A66)
        case .comment: Paper.dynamic(light: 0xA0A1A7, dark: 0x7F848E)
        case .function: Paper.dynamic(light: 0x4078F2, dark: 0x61AFEF)
        case .type: Paper.dynamic(light: 0xC18401, dark: 0xE5C07B)
        }
    }

    /// The code as an attributed string with each token coloured.
    static func highlight(_ code: String, language: String?) -> AttributedString {
        var out = AttributedString()
        for (text, kind) in tokens(code, language: language) {
            var run = AttributedString(text)
            if let color = color(kind) { run.foregroundColor = color }
            if kind == .comment { run.inlinePresentationIntent = .emphasized }
            out += run
        }
        return out
    }

    private static let hashComments: Set<String> = [
        "python", "py", "sh", "bash", "zsh", "shell", "console", "ruby", "rb", "yaml", "yml",
        "toml", "r", "perl", "make", "makefile", "dockerfile", "ini", "conf",
    ]
    private static let plainLanguages: Set<String> = ["text", "txt", "plain", "markdown", "md"]

    static let keywords: Set<String> = [
        // Shared across common languages.
        "if", "else", "elif", "for", "while", "do", "return", "break", "continue", "in", "is",
        "not", "and", "or", "def", "class", "import", "from", "as", "with", "try", "except",
        "finally", "raise", "lambda", "yield", "async", "await", "pass", "global", "nonlocal",
        "assert", "del", "None", "True", "False", "true", "false", "null", "nil", "undefined",
        "let", "var", "const", "func", "function", "fn", "struct", "enum", "interface", "type",
        "impl", "trait", "pub", "mod", "use", "match", "case", "switch", "default", "new",
        "this", "self", "Self", "super", "static", "private", "public", "protected", "final",
        "extends", "implements", "package", "throw", "throws", "catch", "guard", "where",
        "typeof", "instanceof", "export", "then", "fi", "done", "esac", "echo", "mut", "ref",
        "go", "defer", "select", "chan", "map", "of", "void", "int", "float", "bool", "string",
        "extension", "protocol", "init", "deinit", "inout", "some", "any", "override", "end",
    ]

    static func tokens(_ code: String, language: String?) -> [(String, Kind)] {
        let lang = (language ?? "").lowercased()
        if plainLanguages.contains(lang) { return [(code, .plain)] }
        let hash = hashComments.contains(lang)
        let chars = Array(code)
        var out: [(String, Kind)] = []
        var plain = ""
        var i = 0

        func flush() {
            if !plain.isEmpty { out.append((plain, .plain)); plain = "" }
        }
        func emit(_ start: Int, _ end: Int, _ kind: Kind) {
            flush()
            out.append((String(chars[start..<end]), kind))
        }

        while i < chars.count {
            let c = chars[i]
            let next: Character? = i + 1 < chars.count ? chars[i + 1] : nil
            // Comments, to the end of the line (or the block's close).
            if (hash && c == "#") || (!hash && c == "/" && next == "/") {
                let start = i
                while i < chars.count, chars[i] != "\n" { i += 1 }
                emit(start, i, .comment)
                continue
            }
            if !hash, c == "/", next == "*" {
                let start = i
                i += 2
                while i < chars.count, !(chars[i] == "*" && i + 1 < chars.count && chars[i + 1] == "/") {
                    i += 1
                }
                i = min(i + 2, chars.count)
                emit(start, i, .comment)
                continue
            }
            // Strings: '...', "...", `...`, and Python's triple quotes, with escapes.
            if c == "\"" || c == "'" || c == "`" {
                let start = i
                let triple = i + 2 < chars.count && chars[i + 1] == c && chars[i + 2] == c
                i += triple ? 3 : 1
                while i < chars.count {
                    if chars[i] == "\\" { i += 2; continue }
                    if triple {
                        if chars[i] == c, i + 2 < chars.count, chars[i + 1] == c, chars[i + 2] == c {
                            i += 3
                            break
                        }
                    } else if chars[i] == c || (chars[i] == "\n" && c != "`") {
                        i += 1
                        break
                    }
                    i += 1
                }
                i = min(i, chars.count)
                emit(start, i, .string)
                continue
            }
            // Numbers, not inside a word.
            if c.isNumber, plain.last.map({ !($0.isLetter || $0 == "_") }) ?? true {
                let start = i
                while i < chars.count, chars[i].isNumber || chars[i] == "." || chars[i] == "_"
                    || chars[i] == "x" || chars[i].isHexDigit && i > start {
                    i += 1
                }
                emit(start, i, .number)
                continue
            }
            // Words: keywords, calls, and type names.
            if c.isLetter || c == "_" {
                let start = i
                while i < chars.count, chars[i].isLetter || chars[i].isNumber || chars[i] == "_" {
                    i += 1
                }
                let word = String(chars[start..<i])
                // A string prefix such as f"..." or r'...' belongs to the string.
                if word.count <= 2, i < chars.count, chars[i] == "\"" || chars[i] == "'",
                   word.allSatisfy({ "fFrRbBuU".contains($0) }) {
                    let quote = chars[i]
                    i += 1
                    while i < chars.count, chars[i] != quote, chars[i] != "\n" {
                        if chars[i] == "\\" { i += 1 }
                        i += 1
                    }
                    i = min(i + 1, chars.count)
                    emit(start, i, .string)
                    continue
                }
                var j = i
                while j < chars.count, chars[j] == " " { j += 1 }
                let kind: Kind =
                    keywords.contains(word) ? .keyword
                    : j < chars.count && chars[j] == "(" ? .function
                    : word.first?.isUppercase == true && word.count > 1 ? .type
                    : .plain
                if kind == .plain {
                    plain += word
                } else {
                    emit(start, i, kind)
                }
                continue
            }
            plain.append(c)
            i += 1
        }
        flush()
        return out
    }
}
