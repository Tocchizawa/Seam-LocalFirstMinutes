import AppKit
import SwiftUI

#if SWIFT_PACKAGE
import SeamCore
#endif

/// 保存済み議事録を表示する。タブを切り替えても音声操作を使える。
@available(macOS 15.0, *)
public struct MinutesReader: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Stored private var summaryPosition: Int?
    @Stored private var transcriptPosition: Int?
    @Stored private var highlightedTranscriptIndex: Int?
    @Stored private var copyFeedback: UUID?

    private let title: String
    private let date: String
    private let summary: String
    private let sections: [TranscriptSegment]
    private let transcriptLoading: Bool
    private let untimedTranscript: String
    private let query: String
    private let playback: MinutesPlayback?
    private let compact: Bool
    private let animated: Bool
    private let expanded: Bool
    private let toggleExpanded: (() -> Void)?
    private let backTitle: String?
    private let goBack: (() -> Void)?
    private let export: (() -> Void)?
    private let exporting: Bool
    private let menu: AnyView?
    private let activity: BackgroundActivity?
    private let problem: String?
    private let exportNotice: AnyView?
    private let annotations: TranscriptAnnotations?
    private let jump: (id: UUID, time: Double)?
    private let toggleNotes: (() -> Void)?
    private let toggleChat: (() -> Void)?
    private let notesOpen: Bool
    private let chatOpen: Bool

    public struct TranscriptAnnotations {
        let notes: [Double: MeetingNote]
        let edit: (TranscriptSegment, MeetingNote?) -> Void
        let remove: (MeetingNote) -> Void
        let askChat: ((TranscriptSegment) -> Void)?

        public init(notes: [Double: MeetingNote], edit: @escaping (TranscriptSegment, MeetingNote?) -> Void,
                    remove: @escaping (MeetingNote) -> Void, askChat: ((TranscriptSegment) -> Void)? = nil) {
            self.notes = notes
            self.edit = edit
            self.remove = remove
            self.askChat = askChat
        }
    }
    @Binding private var showTranscript: Bool

    public init(
        title: String, date: String, summary: String, sections: [TranscriptSegment], transcriptLoading: Bool = false,
        untimedTranscript: String = "",
        query: String = "", playback: MinutesPlayback? = nil, compact: Bool = false, animated: Bool = true,
        expanded: Bool = false, showTranscript: Binding<Bool>, toggleExpanded: (() -> Void)? = nil,
        backTitle: String? = nil, goBack: (() -> Void)? = nil,
        export: (() -> Void)? = nil, exporting: Bool = false, menu: AnyView? = nil,
        activity: BackgroundActivity? = nil, problem: String? = nil, exportNotice: AnyView? = nil,
        annotations: TranscriptAnnotations? = nil, jump: (id: UUID, time: Double)? = nil,
        toggleNotes: (() -> Void)? = nil, toggleChat: (() -> Void)? = nil,
        notesOpen: Bool = false, chatOpen: Bool = false
    ) {
        self.title = title
        self.date = date
        self.summary = summary
        self.sections = sections
        self.transcriptLoading = transcriptLoading
        self.untimedTranscript = untimedTranscript
        self.query = query
        self.playback = playback
        self.compact = compact
        self.animated = animated
        self.expanded = expanded
        self._showTranscript = showTranscript
        self.toggleExpanded = toggleExpanded
        self.backTitle = backTitle
        self.goBack = goBack
        self.export = export
        self.exporting = exporting
        self.menu = menu
        self.activity = activity
        self.problem = problem
        self.exportNotice = exportNotice
        self.annotations = annotations
        self.jump = jump
        self.toggleNotes = toggleNotes
        self.toggleChat = toggleChat
        self.notesOpen = notesOpen
        self.chatOpen = chatOpen
    }

    private let tint: Color = .primary
    private var motion: Animation? { animated && !reduceMotion ? SeamMotion.update : nil }
    private var padding: CGFloat { compact ? 14 : 33 }
    private var canCopy: Bool {
        showTranscript ? !sections.isEmpty || !untimedTranscript.isEmpty : !summary.isEmpty
    }

    private struct ScrollRequest: Equatable {
        let id: UUID
        let index: Int
    }

    private var scrollRequest: ScrollRequest? {
        guard let jump, let index = sections.transcriptIndex(at: jump.time), showTranscript else { return nil }
        return .init(id: jump.id, index: index)
    }

    public var body: some View {
        // 議事録の読み取り画面。ヘッダー、要約/全文タブ、音声操作を組み合わせる。
        ZStack(alignment: .topLeading) {
            activeTab
                .transition(.identity)
        }
        .padding(.horizontal, padding)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .animatePageChange(for: showTranscript, enabled: animated && !reduceMotion)
        .scrollEdgeBar { header }
        .overlay(alignment: .bottomTrailing) {
            if let exportNotice {
                exportNotice
                    .padding(.trailing, padding)
                    .padding(.bottom, 12)
                    .transition(.opacity.combined(with: .offset(y: 4)))
            }
        }
        .animation(motion, value: exportNotice != nil)
        .scrollEdgeBar(.bottom) {
            if let playback {
                MinutesAudioBar(playback: playback, compact: compact, tint: tint, motion: motion)
                    .padding(.horizontal, padding)
                    .padding(.top, 3)
                    .padding(.bottom, 13)
            }
        }
        .clipped()
        .onChange(of: showTranscript) { _, _ in copyFeedback = nil }
        .task(id: scrollRequest) {
            guard let request = scrollRequest else {
                highlightedTranscriptIndex = nil
                return
            }
            withAnimation(motion) {
                highlightedTranscriptIndex = request.index
                transcriptPosition = request.index
            }
            do { try await Task.sleep(for: .seconds(1.4)) }
            catch { return }
            withAnimation(motion) { highlightedTranscriptIndex = nil }
        }
        .task(id: copyFeedback) {
            guard copyFeedback != nil else { return }
            try? await Task.sleep(for: .seconds(1.25))
            guard !Task.isCancelled else { return }
            copyFeedback = nil
        }
    }

    @ViewBuilder
    // 選択中タブの内容。要約と全文のレイアウトは専用ビューに分けている。
    private var activeTab: some View {
        if showTranscript {
            if transcriptLoading {
                ProgressView("文字起こしを読み込み中…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                transcript
            }
        } else {
            MinutesSummary(markdown: summary, query: query, compact: compact, tint: tint, motion: motion,
                           position: $summaryPosition)
        }
    }

    @ViewBuilder
    private var transcript: some View {
        if let playback {
            PlayingMinutesTranscript(sections: sections, untimed: untimedTranscript, query: query, playback: playback,
                                      compact: compact, tint: tint, motion: motion, position: $transcriptPosition,
                                      annotations: annotations, highlighted: highlightedTranscriptIndex)
        } else {
            MinutesTranscript(sections: sections, untimed: untimedTranscript, query: query, compact: compact,
                              tint: tint, motion: motion, position: $transcriptPosition,
                                      annotations: annotations, highlighted: highlightedTranscriptIndex)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                if let backTitle, let goBack {
                    backButton(backTitle, action: goBack)
                }
                Spacer(minLength: 0)
                toolButtons.seamToolbarGlass()
                    .fixedSize()
            }

            VStack(alignment: .leading, spacing: 5) {
                Text(verbatim: date)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)

                Text(verbatim: title)
                    .font(.system(size: compact ? 24 : 33, weight: .semibold))
                    .lineLimit(compact ? 2 : 3)
                    .accessibilityAddTraits(.isHeader)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            if let activity {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) {
                        tabs
                        activityStatus(activity)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        tabs
                        activityStatus(activity)
                    }
                }
                .transition(.opacity.combined(with: .offset(y: -4)))
            } else {
                tabs
            }

            if let problem {
                Label(problem, systemImage: "exclamationmark.circle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
        .padding(.top, padding)
        .padding(.bottom, 12)
        .padding(.horizontal, padding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .animation(motion, value: activity)
    }

    private var tabs: some View {
        Picker("議事録の表示", selection: $showTranscript) {
            Text("要約").tag(false)
            Text("文字起こし").tag(true)
        }
        .labelsHidden()
        .pickerStyle(.segmented)
        .controlSize(.regular)
        .frame(width: 172)
    }

    private func activityStatus(_ activity: BackgroundActivity) -> some View {
        HStack(spacing: 7) {
            Image(systemName: activity.symbol)
                .symbolEffect(.pulse, options: .repeating, isActive: activity.isRunning && !reduceMotion)
                .accessibilityHidden(true)
            Text(verbatim: activity.stage ?? activity.name)
                .lineLimit(1)
                .contentTransition(.opacity)
            if let progress = activity.progress {
                Text(progress, format: .percent.precision(.fractionLength(0)))
                    .monospacedDigit()
                    .contentTransition(reduceMotion ? .identity : .numericText())
            }
        }
        .font(.system(size: 12, weight: .semibold))
        .foregroundStyle(activity.isRunning ? tint : Color.secondary)
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background((activity.isRunning ? tint : Color.primary).opacity(0.12), in: Capsule())
        .accessibilityElement(children: .combine)
    }

    private func backButton(_ title: String, action: @escaping () -> Void) -> some View {
        let button = Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 11, weight: .semibold))
                    .accessibilityHidden(true)
                Text(verbatim: title)
                    .font(.system(size: 13, weight: .medium))
                    .lineLimit(1)
            }
            .padding(.horizontal, 12)
            .frame(height: SeamToolbarMetrics.height)
            .contentShape(Capsule())
        }
        .buttonStyle(SeamToolbarButtonStyle())
        .accessibilityHint("議事録一覧に戻る")
        .help("議事録一覧に戻る")

        return button.seamToolbarGlass()
    }

    private var toolButtons: some View {
        HStack(spacing: SeamToolbarMetrics.spacing) {
            control(copyFeedback == nil ? "document.on.document" : "checkmark",
                    label: showTranscript ? "文字起こしをコピー" : "要約をコピー", action: copy)
                .opacity(canCopy ? 1 : 0.40)
                .disabled(!canCopy)
                .accessibilityValue(copyFeedback == nil ? "" : "コピーしました")
            if let export {
                control("square.and.arrow.down", label: "ファイルに書き出す", action: export)
                    .opacity(exporting ? 0.40 : 1)
                    .disabled(exporting)
            }
            if let toggleExpanded {
                control(expanded ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right",
                        label: expanded ? "通常表示に戻す" : "議事録を全体表示", action: toggleExpanded)
            }
            if let toggleNotes {
                control(notesOpen ? "tag.fill" : "tag",
                        label: notesOpen ? "メモ・ラベルを閉じる" : "メモ・ラベルを開く", action: toggleNotes)
                    .accessibilityAddTraits(notesOpen ? .isSelected : [])
            }
            if let toggleChat {
                control(chatOpen ? "bubble.left.fill" : "bubble.left",
                        label: chatOpen ? "会議チャットを閉じる" : "会議チャットを開く", action: toggleChat)
                    .accessibilityAddTraits(chatOpen ? .isSelected : [])
            }
            if let menu {
                Divider()
                    .frame(height: 14)
                    .padding(.horizontal, 2)
                    .accessibilityHidden(true)
                menu.frame(width: SeamToolbarMetrics.buttonSize, height: SeamToolbarMetrics.buttonSize)
            }
        }
        .padding(SeamToolbarMetrics.inset)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("議事録のツールバー")
    }

    private func control(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            SeamToolbarIcon(symbol)
        }
        .buttonStyle(SeamToolbarButtonStyle())
        .accessibilityLabel(label)
        .help(label)
    }

    private func copy() {
        let text: String
        if showTranscript {
            text = sections.isEmpty ? untimedTranscript : sections.map { segment in
                [minutesTime(segment.start), segment.text].joined(separator: "  ")
            }.joined(separator: "\n")
        } else {
            text = summary
        }
        NSPasteboard.general.clearContents()
        if NSPasteboard.general.setString(text, forType: .string) {
            withAnimation(motion) { copyFeedback = UUID() }
        }
    }
}

package enum SeamToolbarMetrics {
    package static let buttonSize: CGFloat = 34
    package static let spacing: CGFloat = 2
    package static let inset: CGFloat = 3
    package static var height: CGFloat { buttonSize + 2 * inset }
}

package struct SeamToolbarIcon: View {
    let symbol: String

    package init(_ symbol: String) { self.symbol = symbol }

    package var body: some View {
        Image(systemName: symbol)
            .symbolRenderingMode(.monochrome)
            .font(.system(size: 14, weight: .medium))
            .foregroundStyle(.primary)
            .contentTransition(.opacity)
            .frame(width: SeamToolbarMetrics.buttonSize, height: SeamToolbarMetrics.buttonSize)
            .contentShape(Capsule())
    }
}

package extension View {
    @ViewBuilder func seamToolbarGlass() -> some View {
        if #available(macOS 26.0, *) {
            glassEffect(.regular, in: .capsule)
        } else {
            background(.regularMaterial, in: Capsule())
        }
    }
}

package struct SeamToolbarButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovered = false

    let active: Bool

    package init(active: Bool = false) { self.active = active }

    package func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed
        let highlight = isEnabled ? (pressed ? 0.17 : active ? 0.12 : hovered ? 0.075 : 0) : 0
        configuration.label
            .background(Color.primary.opacity(highlight), in: Capsule())
            .scaleEffect(pressed && !reduceMotion ? 0.94 : 1)
            .animation(reduceMotion ? nil : SeamMotion.feedback, value: pressed)
            .animation(reduceMotion ? nil : SeamMotion.feedback, value: hovered)
            .animation(reduceMotion ? nil : SeamMotion.feedback, value: active)
            .onHover { hovered = $0 }
            .seamActionPointer(isEnabled)
    }
}

#Preview("要約") {
    if #available(macOS 15.0, *) {
        MinutesReader(
            title: "週次プロダクトレビュー",
            date: "2026年11月19日 10:30",
            summary: """
            ## 決定事項

            - 次回リリースは **12月第1週** を目標にする
            - 検索結果にプロジェクト名を表示する

            ## 次のアクション

            UI案を水曜日までに共有する。
            """,
            sections: [], showTranscript: .constant(false)
        )
        .frame(width: 900, height: 720)
        .background(Color.seamPaper(for: .dark))
        .preferredColorScheme(.dark)
    }
}

#Preview("文字起こし") {
    if #available(macOS 15.0, *) {
        MinutesReader(
            title: "週次プロダクトレビュー",
            date: "2026年11月19日 10:30",
            summary: "",
            sections: [
                TranscriptSegment(start: 0, end: 8, text: "では、今週の進捗から確認していきましょう。"),
                TranscriptSegment(start: 8, end: 17, text: "検索画面は一覧と詳細の両方から使えるようにしました。"),
                TranscriptSegment(start: 17, end: 25, text: "いいですね。次のリリースに含める範囲を決めましょう。")
            ],
            showTranscript: .constant(true)
        )
        .frame(width: 900, height: 720)
        .background(Color.seamPaper(for: .dark))
        .preferredColorScheme(.dark)
    }
}

@available(macOS 15.0, *)
private struct PlayingMinutesTranscript: View {
    let sections: [TranscriptSegment]
    let untimed: String
    let query: String
    @ObservedObject var playback: MinutesPlayback
    let compact: Bool
    let tint: Color
    let motion: Animation?
    @Binding var position: Int?
    var annotations: MinutesReader.TranscriptAnnotations?
    var highlighted: Int?

    private var activeSegment: Int? {
        guard playback.hasPosition else { return nil }

        // Transcript segments are stored chronologically. Find the latest segment
        // that has started instead of scanning the full transcript on every player tick.
        var lower = 0
        var upper = sections.count
        while lower < upper {
            let middle = lower + (upper - lower) / 2
            if sections[middle].start <= playback.elapsed {
                lower = middle + 1
            } else {
                upper = middle
            }
        }

        guard lower > 0 else { return nil }
        let index = lower - 1
        return playback.elapsed < sections[index].end ? index : nil
    }

    var body: some View {
        MinutesTranscript(sections: sections, untimed: untimed, query: query, compact: compact,
                          tint: tint, motion: motion, position: $position, annotations: annotations, highlighted: highlighted,
                          active: activeSegment, playing: playback.isPlaying) { time in
            playback.seek(to: time, play: true)
        }
    }
}

@available(macOS 15.0, *)
private struct MinutesTranscript: View {
    private typealias Stored<Value> = SwiftUI.State<Value>

    let sections: [TranscriptSegment]
    let untimed: String
    let query: String
    let compact: Bool
    let tint: Color
    let motion: Animation?
    @Binding var position: Int?
    var annotations: MinutesReader.TranscriptAnnotations?
    var highlighted: Int?
    var active: Int?
    var playing = false
    var seek: ((Double) -> Void)?
    @Stored private var visibleRange: ClosedRange<Int>?

    private var timeWidth: CGFloat {
        (sections.last?.start ?? 0) >= 3600 ? 76 : (compact ? 54 : 61)
    }

    var body: some View {
        SeamScrollView(indicatorInset: 6) {
            LazyVStack(alignment: .leading, spacing: 0) {
                if sections.isEmpty {
                    readerText(AttributedString(untimed.isEmpty ? "文字起こしはまだありません" : untimed),
                               query: query, tint: tint)
                        .foregroundStyle(untimed.isEmpty ? Color.secondary : .primary)
                        .font(.system(size: compact ? 14 : 15.5))
                        .lineSpacing(6)
                        .textSelection(.enabled)
                        .padding(.top, 9)
                }
                ForEach(sections.indices, id: \.self) { index in
                    let line = sections[index]
                    segment(line, active: active == index, selected: highlighted == index)
                        .id(index)
                }
            }
            .scrollTargetLayout()
            .padding(.vertical, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollPosition(id: $position, anchor: .top)
        .onScrollTargetVisibilityChange(idType: Int.self, threshold: 0.30) { ids in
            guard let first = ids.min(), let last = ids.max() else {
                visibleRange = nil
                return
            }
            visibleRange = first...last
        }
        .overlay(alignment: .bottomTrailing) {
            if let active, let visibleRange, playing && !visibleRange.contains(active) {
                Button {
                    withAnimation(motion) { position = active }
                } label: {
                    Label("再生箇所へ", systemImage: "waveform")
                        .font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 11)
                        .frame(height: 31)
                }
                .buttonStyle(.plain)
                .background(.regularMaterial, in: Capsule())
                .padding(6)
            }
        }
        .task(id: query) {
            guard !query.isEmpty, position == nil else { return }
            position = sections.firstIndex {
                $0.text.range(of: query, options: readerSearchOptions) != nil
            }
        }
    }

    private func segment(_ line: TranscriptSegment, active: Bool, selected: Bool) -> some View {
        let note = annotations?.notes[line.start]
        return segmentContent(line, active: active, selected: selected)
            .transcriptNoteMenu(text: line.text, note: note, edit: annotations.map { annotations in
                { annotations.edit(line, note) }
            }, remove: note.flatMap { note in
                annotations.map { annotations in { annotations.remove(note) } }
            }, askChat: annotations?.askChat.map { ask in { ask(line) } })
    }

    @ViewBuilder
    private func segmentContent(_ line: TranscriptSegment, active: Bool, selected: Bool) -> some View {
        if let seek {
            Button { seek(line.start) } label: { row(line, active: active, selected: selected) }
                .buttonStyle(ReaderHoverStyle(tint: tint, motion: motion))
                .accessibilityLabel(rowLabel(line))
                .accessibilityHint("\(minutesTime(line.start)) から再生")
        } else {
            row(line, active: active, selected: selected).textSelection(.enabled)
        }
    }

    private func row(_ line: TranscriptSegment, active: Bool, selected: Bool) -> some View {
        HStack(alignment: .top, spacing: 0) {
            HStack(spacing: 5) {
                Text(minutesTime(line.start))
                    .font(.system(size: 11, weight: active ? .semibold : .medium, design: .monospaced))
                if let note = annotations?.notes[line.start] {
                    MeetingNoteBadge(note: note)
                } else if seek != nil {
                    Image(systemName: active && playing ? "waveform" : "play.fill")
                        .font(.system(size: active && playing ? 10 : 7, weight: .medium))
                        .frame(width: 12, height: 12)
                        .contentTransition(.opacity)
                        .accessibilityHidden(true)
                }
            }
            .foregroundStyle(active ? tint : .secondary)
            .frame(width: timeWidth, alignment: .leading)
            .padding(.leading, 6)
            .padding(.top, 14)

            VStack(alignment: .leading, spacing: 5) {
                readerText(AttributedString(line.text), query: query, tint: tint)
                    .font(.system(size: compact ? 14 : 15.5))
                    .lineSpacing(compact ? 4 : 6)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: 760, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.leading, compact ? 11 : 16)
            .padding(.trailing, 8)
            .padding(.vertical, 12)
            .overlay(alignment: .leading) {
                Rectangle().fill(Color.primary.opacity(0.10)).frame(width: 1)
                Capsule().fill(selected ? Color.accentColor : tint).frame(width: 2)
                    .padding(.vertical, 5)
                    .opacity(active || selected ? 1 : 0)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .background(selected ? Color.accentColor.opacity(0.15) : tint.opacity(active ? 0.075 : 0),
                    in: RoundedRectangle(cornerRadius: 8))
        .animation(motion, value: active)
    }

    private func rowLabel(_ line: TranscriptSegment) -> String {
        [minutesTime(line.start), line.text].joined(separator: "、")
    }

}
