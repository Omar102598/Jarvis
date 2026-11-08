import SwiftUI

// MARK: - Block-level markdown for chat replies
//
// SwiftUI's Text only understands INLINE markdown (bold, italic, code, links),
// so a reply with headings, bullet lists or a table rendered as literal "##"
// and "|---|" noise. This parses the block structure itself and hands each
// block's inline content to AttributedString. Deliberately small: the subset
// Jarvis actually writes, no dependency.

struct MarkdownText: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(Self.parse(text).enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .textSelection(.enabled)
    }

    enum Block {
        case heading(Int, String)
        case paragraph(String)
        case bullets([String])
        case numbered([(String, String)])
        case table([String], [[String]])
        case rule
        case quote(String)
        case code(String)
    }

    @ViewBuilder
    private func view(for block: Block) -> some View {
        switch block {
        case .heading(let level, let t):
            Text(Self.inline(t))
                .font(.system(size: level <= 1 ? 18 : level == 2 ? 16 : 14, weight: .bold))
                .foregroundColor(.jText)
                .padding(.top, 2)
        case .paragraph(let t):
            Text(Self.inline(t))
                .font(.system(size: 14))
                .foregroundColor(.jText)
                .fixedSize(horizontal: false, vertical: true)
        case .bullets(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Text("•").foregroundColor(.jBlue)
                        Text(Self.inline(item))
                            .foregroundColor(.jText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .font(.system(size: 14))
                }
            }
        case .numbered(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Text("\(item.0).")
                            .foregroundColor(.jBlue)
                            .monospacedDigit()
                        Text(Self.inline(item.1))
                            .foregroundColor(.jText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .font(.system(size: 14))
                }
            }
        case .table(let header, let rows):
            VStack(alignment: .leading, spacing: 0) {
                tableRow(header, bold: true)
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    Divider().background(Color.jBorder)
                    tableRow(row, bold: false)
                }
            }
            .padding(10)
            .background(Color.jBg.opacity(0.7))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.jBorder, lineWidth: 1))
            .cornerRadius(8)
        case .rule:
            Divider().background(Color.jBorder)
        case .quote(let t):
            HStack(spacing: 8) {
                Rectangle().fill(Color.jBlue.opacity(0.5)).frame(width: 2)
                Text(Self.inline(t))
                    .font(.system(size: 13))
                    .foregroundColor(.jText.opacity(0.75))
                    .fixedSize(horizontal: false, vertical: true)
            }
        case .code(let t):
            Text(t)
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(.jText)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.jBg)
                .cornerRadius(6)
        }
    }

    private func tableRow(_ cells: [String], bold: Bool) -> some View {
        HStack(alignment: .top, spacing: 8) {
            ForEach(Array(cells.enumerated()), id: \.offset) { i, cell in
                Text(Self.inline(cell))
                    .font(.system(size: bold ? 11 : 12, weight: bold ? .semibold : (i == 0 ? .medium : .regular)))
                    .foregroundColor(bold ? .jText.opacity(0.6) : .jText)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 6)
    }

    // MARK: Parsing

    static func inline(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s,
                               options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(s)
    }

    private static func cells(_ line: String) -> [String] {
        var l = line.trimmingCharacters(in: .whitespaces)
        if l.hasPrefix("|") { l.removeFirst() }
        if l.hasSuffix("|") { l.removeLast() }
        return l.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }

    private static func isTableSeparator(_ line: String) -> Bool {
        let t = line.trimmingCharacters(in: .whitespaces)
        return t.contains("-") && t.allSatisfy { "|-: ".contains($0) }
    }

    static func parse(_ text: String) -> [Block] {
        var blocks: [Block] = []
        var para: [String] = []
        var bullets: [String] = []
        var numbered: [(String, String)] = []
        let lines = text.replacingOccurrences(of: "\r", with: "").components(separatedBy: "\n")

        func flush() {
            if !para.isEmpty { blocks.append(.paragraph(para.joined(separator: "\n"))); para = [] }
            if !bullets.isEmpty { blocks.append(.bullets(bullets)); bullets = [] }
            if !numbered.isEmpty { blocks.append(.numbered(numbered)); numbered = [] }
        }

        var i = 0
        while i < lines.count {
            let raw = lines[i]
            let line = raw.trimmingCharacters(in: .whitespaces)

            if line.hasPrefix("```") {
                flush()
                var code: [String] = []
                i += 1
                while i < lines.count, !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    code.append(lines[i]); i += 1
                }
                blocks.append(.code(code.joined(separator: "\n")))
                i += 1
                continue
            }
            if line.isEmpty { flush(); i += 1; continue }

            // Table: a pipe row followed by a |---| separator row
            if line.contains("|"), i + 1 < lines.count, isTableSeparator(lines[i + 1]) {
                flush()
                let header = cells(line)
                var rows: [[String]] = []
                i += 2
                while i < lines.count, lines[i].contains("|") {
                    rows.append(cells(lines[i])); i += 1
                }
                blocks.append(.table(header, rows))
                continue
            }
            if let m = line.range(of: #"^#{1,6}\s+"#, options: .regularExpression) {
                flush()
                let level = line[m].filter { $0 == "#" }.count
                blocks.append(.heading(level, String(line[m.upperBound...])))
            } else if line == "---" || line == "***" || line == "___" {
                flush(); blocks.append(.rule)
            } else if line.hasPrefix("> ") {
                flush(); blocks.append(.quote(String(line.dropFirst(2))))
            } else if let m = line.range(of: #"^[-*•]\s+"#, options: .regularExpression) {
                if !para.isEmpty || !numbered.isEmpty {
                    let keep = bullets; bullets = []; flush(); bullets = keep
                }
                bullets.append(String(line[m.upperBound...]))
            } else if let m = line.range(of: #"^\d{1,3}[.)]\s+"#, options: .regularExpression) {
                if !para.isEmpty || !bullets.isEmpty {
                    let keep = numbered; numbered = []; flush(); numbered = keep
                }
                let num = line[m].filter(\.isNumber)
                numbered.append((String(num), String(line[m.upperBound...])))
            } else {
                if !bullets.isEmpty || !numbered.isEmpty { flush() }
                para.append(line)
            }
            i += 1
        }
        flush()
        return blocks
    }
}
