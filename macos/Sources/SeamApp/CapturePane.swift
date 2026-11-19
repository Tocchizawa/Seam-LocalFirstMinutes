import SeamCore
import SeamUI
import SwiftUI

@available(macOS 26.0, *)
struct CapturePane: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @ObservedObject var session: WorkspaceSession
    @Binding var expanded: Bool

    private var transition: Animation? { reduceMotion ? nil : SeamMotion.update }
    private var red: Color {
        colorScheme == .dark ? Color(red: 1, green: 0.52, blue: 0.56) : Color(red: 0.68, green: 0.12, blue: 0.20)
    }

    var body: some View {
        if let capture = session.capture {
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
                CaptureTranscript(rows: session.transcript.segments + session.transcript.draftSegments,
                                  expanded: expanded, deferred: capture.deferred, tint: red)
                    .scrollEdgeBar { header(capture) }

                ZStack(alignment: .bottom) {
                    CaptureWaveform(levels: session.levels, tint: red, running: capture.phase == .running)
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
    }

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
            let label = expanded ? "並行表示に戻す" : "文字起こしを全体表示"
            control(expanded ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right",
                    name: label, size: 32) {
                expanded.toggle()
            }
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

    private func transport(_ capture: WorkspaceSession.Capture) -> some View {
        let paused = capture.phase == .paused
        return GlassEffectContainer(spacing: 9) {
            HStack(spacing: 6) {
                HStack(spacing: 4) {
                    let elapsed = minutesTime(capture.elapsed)
                    Text(elapsed)
                        .font(.system(size: 14, weight: .medium, design: .monospaced))
                        .contentTransition(.numericText())
                        .accessibilityLabel("経過時間 \(elapsed)")
                        .animation(transition, value: elapsed)
                        .padding(.leading, 10)
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
            .disabled(capture.phase == .starting || capture.phase == .stopping)
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

@available(macOS 26.0, *)
private struct CaptureTranscript: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Stored private var position = ScrollPosition(edge: .bottom)
    @Stored private var following = true
    @Stored private var scrolling = false
    @Stored private var nearEnd = true
    @Stored private var firstVisible: Double?

    let rows: [TranscriptSegment]
    let expanded: Bool
    let deferred: Bool
    let tint: Color

    var body: some View {
        SeamScrollView {
            LazyVStack(alignment: .leading, spacing: expanded ? 22 : 16) {
                if rows.isEmpty {
                    Text(deferred ? "録音を保存した後に文字起こしを行います" : "発話を待機中")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                ForEach(rows) { row in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack(spacing: 8) {
                            Text(minutesTime(row.start)).monospacedDigit()
                            if let label = row.speakerLabel { Text(verbatim: label) }
                        }
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(row.id == rows.last?.id ? tint : .secondary)

                        TranscriptText(row.text, duration: row.id == rows.last?.id && !reduceMotion ? 0.31 : 0)
                            .font(.system(size: expanded ? 17 : 14))
                            .lineSpacing(expanded ? 6 : 4)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
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
    let levels: [Double]
    let tint: Color
    let running: Bool

    var body: some View {
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
