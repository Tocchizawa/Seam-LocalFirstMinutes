import AppKit
import SwiftUI

#if SWIFT_PACKAGE
import SeamCore
#endif

/// Reads a saved meeting while its audio controls stay available in each tab.
@available(macOS 15.0, *)
public struct MinutesReader: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Namespace private var tabIndicator
    @Stored private var summaryPosition: Int?
    @Stored private var transcriptPosition: Int?

    private let title: String
    private let date: String
    private let summary: String
    private let sections: [TranscriptSegment]
    private let untimedTranscript: String
    private let query: String
    private let playback: MinutesPlayback?
    private let compact: Bool
    private let animated: Bool
    private let expanded: Bool
    private let toggleExpanded: (() -> Void)?
    private let menu: AnyView?
    @Binding private var showTranscript: Bool

    public init(
        title: String, date: String, summary: String, sections: [TranscriptSegment], untimedTranscript: String = "",
        query: String = "", playback: MinutesPlayback? = nil, compact: Bool = false, animated: Bool = true,
        expanded: Bool = false, showTranscript: Binding<Bool>, toggleExpanded: (() -> Void)? = nil, menu: AnyView? = nil
    ) {
        self.title = title
        self.date = date
        self.summary = summary
        self.sections = sections
        self.untimedTranscript = untimedTranscript
        self.query = query
        self.playback = playback
        self.compact = compact
        self.animated = animated
        self.expanded = expanded
        self._showTranscript = showTranscript
        self.toggleExpanded = toggleExpanded
        self.menu = menu
    }

    private var tint: Color {
        colorScheme == .dark ? Color(red: 0.51, green: 0.81, blue: 0.75) : Color(red: 0.14, green: 0.38, blue: 0.34)
    }
    private var motion: Animation? { animated && !reduceMotion ? .easeOut(duration: 0.18) : nil }
    private var padding: CGFloat { compact ? 14 : 33 }

    public var body: some View {
        ZStack(alignment: .topLeading) {
            if showTranscript {
                transcript.transition(paneTransition(x: 5))
            } else {
                MinutesSummary(markdown: summary, query: query, compact: compact, tint: tint, motion: motion,
                               position: $summaryPosition)
                    .transition(paneTransition(x: -5))
            }
        }
        .padding(.horizontal, padding)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .scrollEdgeBar { header }
        .scrollEdgeBar(.bottom) {
            if let playback {
                MinutesAudioBar(playback: playback, compact: compact, tint: tint, motion: motion)
                    .padding(.horizontal, padding)
                    .padding(.top, 3)
                    .padding(.bottom, 13)
            }
        }
        .clipped()
        .animation(motion, value: showTranscript)
    }

    @ViewBuilder
    private var transcript: some View {
        if let playback {
            PlayingMinutesTranscript(sections: sections, untimed: untimedTranscript, query: query, playback: playback,
                                      compact: compact, tint: tint, motion: motion, position: $transcriptPosition)
        } else {
            MinutesTranscript(sections: sections, untimed: untimedTranscript, query: query, compact: compact,
                              tint: tint, motion: motion, position: $transcriptPosition)
        }
    }

    private func paneTransition(x: CGFloat) -> AnyTransition {
        guard let motion else { return .identity }
        return .asymmetric(insertion: .opacity.combined(with: .offset(x: x)).animation(motion), removal: .identity)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(verbatim: title)
                .font(.system(size: compact ? 24 : 33, weight: .semibold))
                .lineLimit(compact ? 2 : 3)
                .accessibilityAddTraits(.isHeader)
                .padding(.bottom, 7)
            Text(verbatim: date)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)

            HStack(spacing: 2) {
                tab("要約", transcript: false)
                tab("文字起こし", transcript: true)
                Spacer(minLength: 0)
                if let toggleExpanded {
                    let label = expanded ? "通常表示に戻す" : (showTranscript ? "文字起こしを全体表示" : "議事録を全体表示")
                    control(expanded ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right",
                            label: label, action: toggleExpanded)
                }
                control("document.on.document", label: showTranscript ? "文字起こしをコピー" : "要約をコピー", action: copy)
                    .disabled(showTranscript ? sections.isEmpty && untimedTranscript.isEmpty : summary.isEmpty)
                if let menu { menu.frame(width: 33, height: 33) }
            }
            .padding(.top, compact ? 10 : 14)
            .background(alignment: .bottom) {
                Rectangle().fill(Color.primary.opacity(0.10)).frame(height: 1)
            }
        }
        .padding(.top, compact ? 9 : 16)
        .padding(.horizontal, padding)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func tab(_ label: String, transcript: Bool) -> some View {
        let selected = showTranscript == transcript
        return Button { showTranscript = transcript } label: {
            Text(label)
                .font(.system(size: 13, weight: selected ? .semibold : .medium))
                .foregroundStyle(selected ? tint : Color.secondary)
                .frame(minWidth: 68, minHeight: 40)
                .padding(.horizontal, 5)
                .contentShape(Rectangle())
                .overlay(alignment: .bottom) {
                    if selected {
                        Capsule()
                            .fill(tint)
                            .frame(height: 2)
                            .matchedGeometryEffect(id: "selectedTab", in: tabIndicator)
                    }
                }
        }
        .buttonStyle(ReaderHoverStyle(tint: tint, motion: motion))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func control(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(.secondary)
                .contentTransition(.opacity)
                .frame(width: 33, height: 33)
                .contentShape(Rectangle())
        }
        .buttonStyle(ReaderHoverStyle(tint: tint, motion: motion))
        .accessibilityLabel(label)
        .help(label)
    }

    private func copy() {
        let text: String
        if showTranscript {
            text = sections.isEmpty ? untimedTranscript : sections.map { segment in
                [minutesTime(segment.start), segment.speakerLabel, segment.text]
                    .compactMap { $0 }
                    .joined(separator: "  ")
            }.joined(separator: "\n")
        } else {
            text = summary
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
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

    var body: some View {
        let active = playback.hasPosition ? sections.lastIndex {
            playback.elapsed >= $0.start && playback.elapsed < $0.end
        } : nil
        MinutesTranscript(sections: sections, untimed: untimed, query: query, compact: compact,
                          tint: tint, motion: motion, position: $position, active: active, playing: playback.isPlaying) { time in
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
    var active: Int?
    var playing = false
    var seek: ((Double) -> Void)?
    @Stored private var visible = Set<Int>()

    private var timeWidth: CGFloat {
        (sections.last?.start ?? 0) >= 3600 ? 76 : (compact ? 54 : 61)
    }

    var body: some View {
        ScrollView {
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
                    let startsTurn = index == 0 || sections[index - 1].speakerLabel != line.speakerLabel
                        || line.start - sections[index - 1].end > 12
                    segment(line, active: active == index, showSpeaker: startsTurn)
                        .id(index)
                }
            }
            .scrollTargetLayout()
            .padding(.vertical, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollPosition(id: $position, anchor: .top)
        .onScrollTargetVisibilityChange(idType: Int.self, threshold: 0.30) { visible = Set($0) }
        .overlay(alignment: .bottomTrailing) {
            if let active, playing && !visible.isEmpty && !visible.contains(active) {
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

    @ViewBuilder
    private func segment(_ line: TranscriptSegment, active: Bool, showSpeaker: Bool) -> some View {
        if let seek {
            Button { seek(line.start) } label: { row(line, active: active, showSpeaker: showSpeaker) }
                .buttonStyle(ReaderHoverStyle(tint: tint, motion: motion))
                .accessibilityLabel(rowLabel(line))
                .accessibilityHint("\(minutesTime(line.start)) から再生")
                .help("\(minutesTime(line.start)) から再生")
                .contextMenu {
                    Button("発言をコピー") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(line.text, forType: .string)
                    }
                }
        } else {
            row(line, active: active, showSpeaker: showSpeaker).textSelection(.enabled)
        }
    }

    private func row(_ line: TranscriptSegment, active: Bool, showSpeaker: Bool) -> some View {
        HStack(alignment: .top, spacing: 0) {
            HStack(spacing: 5) {
                Text(minutesTime(line.start))
                    .font(.system(size: 11, weight: active ? .semibold : .medium, design: .monospaced))
                if seek != nil {
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
                if showSpeaker, let speaker = line.speakerLabel, !speaker.isEmpty {
                    Text(verbatim: speaker)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(active ? tint : .secondary)
                }
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
                Capsule().fill(tint).frame(width: 2)
                    .padding(.vertical, 5)
                    .opacity(active ? 1 : 0)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .background(tint.opacity(active ? 0.075 : 0), in: RoundedRectangle(cornerRadius: 8))
        .animation(motion, value: active)
        .animation(motion, value: playing)
    }

    private func rowLabel(_ line: TranscriptSegment) -> String {
        [minutesTime(line.start), line.speakerLabel, line.text]
            .compactMap { $0 }
            .joined(separator: "、")
    }

}
