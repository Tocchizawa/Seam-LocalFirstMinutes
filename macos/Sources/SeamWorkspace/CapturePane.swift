import SeamCore
import SeamUI
import SwiftUI

/// 録音中に表示する文字起こし、波形、録音操作の画面。
@available(macOS 26.0, *)
struct CapturePane: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @AppStorage("meetingChatWidth") private var chatWidth = 0.0
    @State private var microphoneSettingsOpen = false
    @StateObject private var notebook = MeetingNotes()
    @State private var transcriptQuestion: MeetingTranscriptQuestion?
    @State private var jump: (id: UUID, time: Double)?
    @ObservedObject var session: WorkspaceSession
    @Binding var expanded: Bool
    @Binding var chatOpen: Bool

    private var transition: Animation? { reduceMotion ? nil : SeamMotion.update }
    private var red: Color {
        // 録音中の強調色。ライト/ダークで読みやすさを保つため別々に指定する。
        colorScheme == .dark ? Color(red: 1, green: 0.52, blue: 0.56) : Color(red: 0.68, green: 0.12, blue: 0.20)
    }

    var body: some View {
        if let capture = session.capture {
            MeetingSplitView(showing: chatOpen && capture.id != nil, preferredWidth: $chatWidth) {
                transcriptArea(capture)
            } side: { _ in
                if let id = capture.id {
                    let inProgress = capture.phase == .running || capture.phase == .paused
                    MeetingChatPanel(api: session.api, session: id, showsQuickQuestions: inProgress,
                                     transcriptQuestion: $transcriptQuestion, jump: { jump = (UUID(), $0) }, close: { chatOpen = false })
                }
            }
            .task(id: "\(capture.id ?? ""):\(session.meetingNotesRevision):\(session.connected)") {
                if let id = capture.id {
                    await notebook.load(via: session.api, session: id)
                }
            }
            .sheet(item: $notebook.editor) { editing in
                if let id = capture.id {
                    MeetingNoteEditor(editing: editing) { title, body in
                        try await notebook.save(editing, title: title, body: body, via: session.api, session: id)
                    }
                }
            }
            .alert("メモ", isPresented: Binding(
                get: { notebook.problem != nil },
                set: { if !$0 { notebook.problem = nil } }
            )) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(notebook.problem ?? "")
            }
        }
    }

    private func transcriptArea(_ capture: WorkspaceSession.Capture) -> some View {
        VStack(spacing: 16) {
            if let reminder = session.idleReminder, capture.phase == .running {
                HStack(spacing: 8) {
                    Label("\(reminder.silentForSec)秒間、音声入力がありません", systemImage: "bell")
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button {
                        session.idleReminder = nil
                    } label: {
                        Image(systemName: "xmark")
                            .frame(width: 28, height: 28)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("録音の通知を閉じる")
                }
                .font(.callout)
                .padding(.leading, 12)
                .padding(.trailing, 3)
                .padding(.vertical, 3)
                .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 9))
            }
            let confirmed = session.transcript.segments
            CaptureTranscript(rows: confirmed + session.transcript.draftSegments,
                              lastFinalizedStart: confirmed.last?.start,
                              expanded: expanded, deferred: capture.deferred, tint: red,
                              notes: notebook.byStart(in: confirmed), jump: jump,
                              editNote: { row, note in notebook.editor = .init(start: row.start, note: note) },
                              removeNote: { note in
                                  guard let id = capture.id else { return }
                                  Task { await notebook.delete(note, via: session.api, session: id) }
                              }, askChat: { row in
                                  chatOpen = true
                                  transcriptQuestion = .init(start: row.start)
                              })
                .scrollEdgeBar { header(capture) }

            ZStack(alignment: .bottom) {
                CaptureWaveform(meter: session.audioMeter, tint: red, running: capture.phase == .running)
                    .frame(height: expanded ? 100 : 82)
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)

                transport(capture)
            }
        }
        .padding(.horizontal, expanded ? 34 : 19)
        .padding(.bottom, 19)
        .frame(maxWidth: expanded ? 850 : .infinity, maxHeight: .infinity)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // 録音状態と文字起こしの全体表示切り替えを置く上部バー。
    private func header(_ capture: WorkspaceSession.Capture) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Circle().fill(capture.phase == .running ? red : .secondary)
                        .frame(width: 6, height: 6)
                        .accessibilityHidden(true)
                    Text(status(capture.phase))
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(capture.phase == .running ? red : .secondary)
                        .contentTransition(.opacity)
                }
                Text("文字起こし")
                    .font(.system(size: expanded ? 27 : 19, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
            }
            Spacer()
            let transcriptOnly = expanded && !chatOpen
            let label = transcriptOnly ? "並行表示に戻す" : "文字起こしを全体表示"
            control(transcriptOnly ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right",
                    name: label, size: 32) {
                expanded = !transcriptOnly
                chatOpen = false
            }
            control(chatOpen ? "bubble.left.fill" : "bubble.left",
                    name: chatOpen ? "会議チャットを閉じる" : "会議チャットを開く", size: 32) {
                chatOpen.toggle()
            }
            .accessibilityAddTraits(chatOpen ? .isSelected : [])
            .disabled(capture.id == nil)
        }
        .padding(.top, 26)
        .padding(.bottom, 12)
    }

    private func status(_ phase: WorkspaceSession.Capture.Phase) -> String {
        switch phase {
        case .starting: "録音を準備中"
        case .running: "録音中"
        case .paused: "一時停止中"
        case .stopping: "録音を保存中"
        }
    }

    // 経過時間、マイク、再開/一時停止、停止の操作列。
    private func transport(_ capture: WorkspaceSession.Capture) -> some View {
        let paused = capture.phase == .paused
        let elapsed = minutesTime(capture.elapsed)
        return GlassEffectContainer(spacing: 4) {
            HStack(spacing: 6) {
                Text(elapsed)
                    .font(.system(size: 14, weight: .medium, design: .monospaced))
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                    .contentTransition(.numericText())
                    .accessibilityLabel("経過時間 \(elapsed)")
                    .animation(transition, value: elapsed)
                    .padding(.horizontal, 8)
                    .frame(height: 42)
                    .glassEffect(.regular, in: .capsule)

                HStack(spacing: 0) {
                    Button {
                        Task { await session.toggleMicrophone() }
                    } label: {
                        Image(systemName: capture.muted ? "mic.slash" : "mic")
                            .font(.system(size: 13, weight: .medium))
                            .contentTransition(.symbolEffect(.replace))
                            .frame(width: 33, height: 39)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(capture.muted ? "ミュートを解除" : "マイクをミュート")
                    .help(capture.muted ? "ミュートを解除" : "マイクをミュート")

                    Button {
                        microphoneSettingsOpen.toggle()
                    } label: {
                        Image(systemName: "chevron.down")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 22, height: 39)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(SeamToolbarButtonStyle(active: microphoneSettingsOpen))
                    .accessibilityLabel("マイクの入力設定")
                    .help("マイクの入力設定")
                    .popover(isPresented: $microphoneSettingsOpen, arrowEdge: .top) {
                        MicrophoneInputSettings(session: session)
                    }
                }
                .padding(.trailing, 3)
                .frame(height: 42)
                .glassEffect(.regular, in: .capsule)
                control(paused ? "play.fill" : "pause.fill", name: paused ? "再開" : "一時停止", size: 42) {
                    Task { await session.setPaused() }
                }
                control("stop.fill", name: "停止", size: 42, destructive: true) {
                    Task { await session.stop() }
                }
            }
            .disabled(capture.phase == .starting || capture.phase == .stopping
                      || session.isChanging || session.switchingMicrophone)
            .animation(transition, value: capture.muted)
            .animation(transition, value: capture.phase)
        }
    }

    private func control(_ symbol: String, name: String, size: CGFloat, destructive: Bool = false,
                         action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(destructive ? red : .primary)
                .contentTransition(.symbolEffect(.replace))
                .frame(width: size, height: size)
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .glassEffect(destructive ? .regular.tint(red.opacity(0.12)).interactive() : .regular.interactive(), in: .circle)
        .accessibilityLabel(name)
        .help(name)
    }
}

private struct MicrophoneInputSettings: View {
    @ObservedObject var session: WorkspaceSession
    @State private var devices: [RecordingMicrophone]?
    @State private var problem: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("マイク入力").font(.headline).accessibilityAddTraits(.isHeader)

            if let devices, let capture = session.capture {
                SeamDropdownSelection("入力元",
                    selection: Binding(get: { session.capture?.microphoneUID }, set: select),
                    options: [(nil, "システムの既定")] + devices.map { (Optional($0.uid), $0.name) },
                    unknown: capture.microphoneName ?? "接続されていないマイク")
                .disabled(session.switchingMicrophone || session.isChanging || capture.phase == .stopping)
                if session.switchingMicrophone {
                    Text("マイクを切り替え中…").font(.caption).foregroundStyle(.secondary)
                }
            } else if session.capture?.canSwitchMicrophone != true {
                if let currentName = session.capture?.microphoneName {
                    LabeledContent("使用中", value: currentName)
                }
                Text("録音中の入力切り替えにはCore Audioが必要です。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if problem == nil {
                ProgressView().controlSize(.small).accessibilityLabel("マイクを読み込み中")
            }

            if let problem {
                Text(problem).font(.caption).foregroundStyle(.secondary)
                if devices == nil {
                    Button("再読み込み") { Task { await load() } }
                }
            }
        }
        .padding(15)
        .frame(width: 300, alignment: .leading)
        .task { await load() }
    }

    private func load() async {
        guard session.capture?.canSwitchMicrophone == true else { return }
        problem = nil
        do {
            let available = try await session.api.recordingMicrophones()
            guard !Task.isCancelled else { return }
            devices = available
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func select(_ deviceUID: String?) {
        guard !session.switchingMicrophone else { return }
        problem = nil
        Task {
            do {
                try await session.switchMicrophone(deviceUID)
            } catch {
                problem = error.localizedDescription
            }
        }
    }
}

@available(macOS 26.0, *)
private struct CaptureTranscript: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Stored private var position = ScrollPosition(edge: .bottom)
    @Stored private var following = true
    @Stored private var scrolling = false
    @Stored private var nearEnd = true
    @Stored private var firstVisible: Double?
    @Stored private var highlightedStart: Double?

    let rows: [TranscriptSegment]
    let lastFinalizedStart: Double?
    let expanded: Bool
    let deferred: Bool
    let tint: Color
    let notes: [Double: MeetingNote]
    let jump: (id: UUID, time: Double)?
    let editNote: (TranscriptSegment, MeetingNote?) -> Void
    let removeNote: (MeetingNote) -> Void
    let askChat: (TranscriptSegment) -> Void

    var body: some View {
        // 新しい発話を自動追従する。手動でスクロールした場合は位置を保つ。
        SeamScrollView(indicatorInset: 6) {
            LazyVStack(alignment: .leading, spacing: expanded ? 22 : 16) {
                if rows.isEmpty {
                    Text(deferred ? "確定版は録音後に作成します" : "発話を待機中")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                ForEach(rows) { row in
                    let isDraft = row.start > (lastFinalizedStart ?? -.infinity)
                    let note = notes[row.start]
                    let highlighted = row.start == highlightedStart
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(spacing: 8) {
                            Text(minutesTime(row.start)).monospacedDigit()
                            if let note { MeetingNoteBadge(note: note) }
                        }
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(row.id == rows.last?.id ? tint : .secondary)

                        TranscriptText(row.text, duration: !isDraft && row.id == rows.last?.id && !reduceMotion ? 0.31 : 0)
                            .font(.system(size: expanded ? 17 : 14))
                            .foregroundStyle(isDraft ? .secondary : .primary)
                            .lineSpacing(expanded ? 6 : 4)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(Color.accentColor.opacity(highlighted ? 0.15 : 0), in: .rect(cornerRadius: 8))
                    .overlay(alignment: .leading) {
                        Capsule()
                            .fill(Color.accentColor)
                            .frame(width: 2)
                            .padding(.leading, 2)
                            .padding(.vertical, 6)
                            .opacity(highlighted ? 1 : 0)
                            .allowsHitTesting(false)
                            .accessibilityHidden(true)
                    }
                    .padding(-8)
                    .contentShape(Rectangle())
                    .transcriptNoteMenu(text: row.text, note: note, draft: isDraft,
                                        edit: { editNote(row, note) }, remove: {
                                            if let note { removeNote(note) }
                                        }, askChat: { askChat(row) })
                    .id(row.id)
                }
            }
            .scrollTargetLayout()
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.bottom, 6)
        }
        .defaultScrollAnchor(.bottom, for: .initialOffset)
        .scrollPosition($position)
        .onScrollTargetVisibilityChange(idType: Double.self, threshold: 0.15) { firstVisible = $0.min() }
        .onScrollPhaseChange { _, phase in
            switch phase {
            case .interacting, .tracking, .decelerating: scrolling = true
            case .idle:
                if scrolling { following = nearEnd }
                scrolling = false
            default: break
            }
        }
        .onScrollGeometryChange(for: Bool.self) {
            $0.visibleRect.maxY >= $0.contentSize.height - 32
        } action: { _, end in
            nearEnd = end
            if scrolling { following = end }
        }
        .onChange(of: rows.last) {
            if following && !scrolling { jumpToEnd() }
        }
        .task(id: jump?.id) {
            guard let jump, let index = rows.transcriptIndex(at: jump.time) else { return }
            following = false
            let start = rows[index].id
            withAnimation(reduceMotion ? nil : SeamMotion.update) {
                highlightedStart = start
                position.scrollTo(id: start, anchor: .top)
            }
            do { try await Task.sleep(for: .seconds(1.4)) }
            catch { return }
            withAnimation(reduceMotion ? nil : SeamMotion.update) { highlightedStart = nil }
        }
        .onChange(of: expanded) {
            if following { jumpToEnd() }
            else if let firstVisible { position.scrollTo(id: firstVisible, anchor: .top) }
        }
        .overlay(alignment: .bottomTrailing) {
            if !following {
                Button {
                    following = true
                    jumpToEnd()
                } label: {
                    Label("最新へ", systemImage: "arrow.down")
                        .font(.system(size: 12, weight: .medium))
                        .padding(.horizontal, 10)
                        .frame(height: 32)
                }
                .buttonStyle(.plain)
                .glassEffect(.regular.interactive(), in: .capsule)
                .padding(4)
            }
        }
    }

    private func jumpToEnd() {
        withAnimation(reduceMotion ? nil : SeamMotion.update) { position.scrollTo(edge: .bottom) }
    }
}

private struct CaptureWaveform: View {
    @ObservedObject var meter: AudioLevelMeter
    let tint: Color
    let running: Bool

    var body: some View {
        let levels = meter.samples
        // 波形の密度や振れ幅は step と barHeight の計算で調整する。
        Canvas { context, geometry in
            let step = 5.2
            let count = max(2, Int(geometry.width / step))
            let xMargin = (geometry.width - Double(count - 1) * step) / 2
            for column in 0..<count {
                let progress = Double(column) / Double(count - 1)
                let envelope = pow(max(0, sin(progress * .pi)), 0.76)
                let sample = Int((progress * 63).rounded()) - (64 - levels.count)
                let peak = running && levels.indices.contains(sample) ? levels[sample] : 0
                let height = Int(envelope * sqrt(peak) * 8)
                for point in -height...height {
                    let vertical = 1 - Double(abs(point)) / Double(max(4, height + 2))
                    let opacity = envelope * vertical * (running ? 0.62 : 0.19)
                    let x = xMargin + Double(column) * step
                    let y = geometry.height * 0.43 + Double(point) * step
                    let dot = CGRect(x: x, y: y, width: 2.5, height: 2.5)
                    context.fill(Path(ellipseIn: dot), with: .color((running ? tint : .secondary).opacity(opacity)))
                }
            }
        }
    }
}
