import SwiftUI

private struct SummaryBlock: Identifiable {
    let id: Int
    let intents: [PresentationIntent.IntentType]
    let text: AttributedString
    var rows: [[AttributedString]] = []

    var kind: PresentationIntent.Kind { intents.first?.kind ?? .paragraph }
    var heading: String? {
        guard case let .header(level) = kind, level <= 2 else { return nil }
        return String(text.characters)
    }

    static func read(_ markdown: String) -> [Self] {
        guard !markdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return [] }
        guard let parsed = try? AttributedString(markdown: markdown, options: .init(interpretedSyntax: .full)) else {
            return [Self(id: 0, intents: [], text: AttributedString(markdown))]
        }

        var blocks: [Self] = []
        var tableRowID: Int?
        for (intent, range) in parsed.runs[\.presentationIntent] {
            var content = AttributedString(parsed[range])
            content.presentationIntent = nil
            let intents = intent?.components ?? []
            // Markdown table cells have their own intent. Keep each table
            // together so it can share aligned columns across its rows.
            if intents.count >= 3, case .tableCell = intents[0].kind, case .table = intents[2].kind {
                let table = intents[2]
                if blocks.last?.intents.first != table {
                    blocks.append(Self(id: blocks.count, intents: [table], text: AttributedString()))
                    tableRowID = nil
                }
                let blockIndex = blocks.count - 1
                if tableRowID != intents[1].identity {
                    blocks[blockIndex].rows.append([])
                    tableRowID = intents[1].identity
                }
                let row = blocks[blockIndex].rows.count - 1
                blocks[blockIndex].rows[row].append(content)
            } else {
                blocks.append(Self(id: blocks.count, intents: intents, text: content))
            }
        }

        return blocks.isEmpty ? [Self(id: 0, intents: [], text: AttributedString(markdown))] : blocks
    }
}

@available(macOS 14.0, *)
struct MinutesSummary: View {
    private typealias Stored<Value> = SwiftUI.State<Value>

    let markdown: String
    let query: String
    let compact: Bool
    let tint: Color
    let motion: Animation?
    @Binding var position: Int?

    @Stored private var blocks: [SummaryBlock] = []

    var body: some View {
        GeometryReader { geometry in
            let headings = blocks.filter { $0.heading != nil }
            HStack(alignment: .top, spacing: 32) {
                SeamScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        if blocks.isEmpty && markdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                            Text("要約はまだありません")
                                .foregroundStyle(.secondary)
                        }
                        ForEach(blocks) { block in
                            content(of: block)
                                .id(block.id)
                        }
                    }
                    .scrollTargetLayout()
                    .font(.system(size: compact ? 14 : 15))
                    .lineSpacing(compact ? 5 : 7)
                    .textSelection(.enabled)
                    .padding(.top, 22)
                    .padding(.bottom, 27)
                    .frame(maxWidth: 760, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .scrollPosition(id: $position, anchor: .top)

                if !compact && geometry.size.width >= 630 && headings.count >= 3 {
                    outline(headings)
                }
            }
        }
        .task(id: markdown) {
            blocks = SummaryBlock.read(markdown)
            if position == nil { revealMatch() }
        }
        .task(id: query) { revealMatch() }
    }

    private func outline(_ headings: [SummaryBlock]) -> some View {
        let active = headings.last { $0.id <= (position ?? 0) }?.id
        return SeamScrollView {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(headings) { block in
                    Button {
                        withAnimation(motion) { position = block.id }
                    } label: {
                        Text(verbatim: block.heading ?? "")
                            .font(.system(size: 12, weight: block.id == active ? .semibold : .medium))
                            .foregroundStyle(block.id == active ? tint : .secondary)
                            .padding(.horizontal, 11)
                            .padding(.vertical, 7)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(ReaderHoverStyle(tint: tint, motion: motion))
                }
            }
            .padding(.leading, 4)
            .overlay(alignment: .leading) { Rectangle().fill(.quaternary).frame(width: 1) }
            .padding(.top, 24)
        }
        .scrollIndicators(.hidden)
        .frame(width: 148)
        .accessibilityLabel("要約の目次")
    }

    @ViewBuilder
    private func content(of block: SummaryBlock) -> some View {
        switch block.kind {
        case .header(let level):
            readerText(block.text, query: query, tint: tint)
                .font(.system(size: level <= 2 ? (compact ? 17 : 20) : 15, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
                .padding(.top, block.id == 0 ? 0 : (level <= 2 ? 13 : 5))
        case .codeBlock:
            SeamScrollView(.horizontal) {
                readerText(block.text, query: query, tint: tint)
                    .font(.system(.footnote, design: .monospaced))
                    .fixedSize()
                    .padding(12)
            }
            .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))
        case .thematicBreak:
            Divider().padding(.vertical, 5)
        case .table:
            table(block.rows)
        default:
            paragraph(block)
        }
    }

    @ViewBuilder
    private func paragraph(_ block: SummaryBlock) -> some View {
        let items = block.intents.filter {
            if case .listItem = $0.kind { return true }
            return false
        }
        let quoted = block.intents.contains { $0.kind == .blockQuote }
        let text = readerText(block.text, query: query, tint: tint)
            .foregroundStyle(Color.primary.opacity(0.90))
            .fixedSize(horizontal: false, vertical: true)
        if let item = items.first, case let .listItem(ordinal) = item.kind {
            let container = block.intents.drop(while: { $0 != item }).dropFirst().first
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(verbatim: container?.kind == .orderedList ? "\(ordinal)." : "•")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                    .frame(minWidth: 10, alignment: .trailing)
                text
            }
            .padding(.leading, CGFloat(items.count - 1) * 18)
        } else if quoted {
            text
                .padding(.leading, 13)
                .overlay(alignment: .leading) { Capsule().fill(tint.opacity(0.4)).frame(width: 2) }
        } else {
            text
        }
    }

    private func table(_ rows: [[AttributedString]]) -> some View {
        SeamScrollView(.horizontal) {
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 0) {
                ForEach(rows.indices, id: \.self) { row in
                    GridRow(alignment: .top) {
                        ForEach(rows[row].indices, id: \.self) { column in
                            readerText(rows[row][column], query: query, tint: tint)
                                .font(.system(size: 13, weight: row == 0 ? .semibold : .regular))
                                .padding(.vertical, 7)
                        }
                    }
                    Divider().gridCellUnsizedAxes(.horizontal)
                }
            }
        }
    }

    private func revealMatch() {
        guard !query.isEmpty, let target = blocks.first(where: {
            $0.text.range(of: query, options: readerSearchOptions) != nil ||
            $0.rows.joined().contains { $0.range(of: query, options: readerSearchOptions) != nil }
        }) else { return }
        position = target.id
    }
}

let readerSearchOptions: String.CompareOptions = [.caseInsensitive, .diacriticInsensitive, .widthInsensitive]

func readerText(_ original: AttributedString, query: String, tint: Color) -> Text {
    var text = original
    if !query.isEmpty, let hit = text.range(of: query, options: readerSearchOptions) {
        text[hit].backgroundColor = tint.opacity(0.17)
    }
    return Text(text)
}

public struct ReaderHoverStyle: ButtonStyle {
    let tint: Color
    let motion: Animation?

    public init(tint: Color, motion: Animation?) {
        self.tint = tint
        self.motion = motion
    }

    public func makeBody(configuration: Configuration) -> some View {
        Hoverable(label: configuration.label, pressed: configuration.isPressed, tint: tint, motion: motion)
    }

    private struct Hoverable<Label: View>: View {
        private typealias Stored<Value> = SwiftUI.State<Value>
        @Stored private var hover = false
        let label: Label
        let pressed: Bool
        let tint: Color
        let motion: Animation?

        var body: some View {
            label
                .background {
                    RoundedRectangle(cornerRadius: 8)
                        .fill(tint.opacity(pressed ? 0.14 : (hover ? 0.075 : 0)))
                }
                .animation(motion, value: hover)
                .animation(motion, value: pressed)
                .onHover { hover = $0 }
        }
    }
}
