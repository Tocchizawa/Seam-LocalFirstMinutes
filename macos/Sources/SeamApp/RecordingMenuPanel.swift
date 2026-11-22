import AppKit
import SeamUI
import SeamWorkspace
import SwiftUI

@available(macOS 26.0, *)
struct RecordingMenuPanel: View {
    static let width: CGFloat = 330

    @ObservedObject var service: LocalBackend
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let openWorkspace: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let session = service.session {
                SessionMenuSections(session: session)
            } else {
                switch service.state {
                case .failed:
                    HStack(spacing: 8) {
                        Label("録音サービスを起動できませんでした", systemImage: "exclamationmark.circle")
                            .font(.callout)
                        Spacer(minLength: 0)
                        Button("再試行", action: service.start)
                            .buttonStyle(.glass)
                    }
                case .preparing, .ready:
                    loadingStatus("録音サービスを準備中")
                }
            }

            Divider()
            Button(action: openWorkspace) {
                Label("Seamを開く", systemImage: "macwindow")
                    .frame(maxWidth: .infinity, minHeight: 36, alignment: .leading)
                    .overlay(alignment: .trailing) {
                        Image(systemName: "arrow.up.right")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                    }
                    .padding(.horizontal, 8)
                    .contentShape(Rectangle())
            }
            .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
        }
        .padding(14)
        .tint(.primary)
        .frame(width: Self.width, alignment: .topLeading)
        .fixedSize(horizontal: false, vertical: true)
    }
}

@available(macOS 26.0, *)
private struct SessionMenuSections: View {
    @ObservedObject var session: WorkspaceSession

    var body: some View {
        let capture = session.capture
        let currentActivityID = capture?.id.map { "transcribe-\($0)" }
        let activities = session.backgroundActivities.filter { $0.id != currentActivityID }
        if let capture {
            switch capture.phase {
            case .starting:
                loadingStatus(capture.phase.menuStatus)
            case .running, .paused, .stopping:
                RecordingControls(capture: capture, session: session)
            }
        } else if !session.connected {
            loadingStatus("録音サービスに接続中")
        } else if activities.isEmpty, session.error == nil {
            Label("録音していません", systemImage: "waveform")
                .font(.callout)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 8)
                .padding(.vertical, 5)
        }

        if let message = session.error {
            MenuErrorNotice(message: message) { session.error = nil }
        }
        if !activities.isEmpty {
            MenuActivities(activities: activities)
        }
    }
}

@available(macOS 26.0, *)
private struct RecordingControls: View {
    let capture: WorkspaceSession.Capture
    let session: WorkspaceSession
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var recordingTint: Color {
        colorScheme == .dark ? Color(red: 1, green: 0.52, blue: 0.56) : Color(red: 0.68, green: 0.12, blue: 0.20)
    }

    var body: some View {
        let stopping = capture.phase == .stopping
        let paused = capture.phase == .paused
        let time = minutesTime(capture.elapsed)
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    Label(capture.phase.menuStatus, systemImage: capture.phase.menuSymbol)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(capture.phase == .running ? recordingTint : Color.secondary)
                    Text(time)
                        .font(.system(size: 28, weight: .medium))
                        .monospacedDigit()
                        .lineLimit(1)
                        .minimumScaleFactor(0.75)
                        .contentTransition(.numericText())
                        .animation(reduceMotion ? nil : SeamMotion.feedback, value: time)
                        .accessibilityLabel("録音時間")
                        .accessibilityValue(time)
                }
                Spacer(minLength: 0)
                if stopping {
                    ProgressView()
                        .controlSize(.small)
                        .padding(.trailing, 8)
                        .accessibilityLabel("録音を保存中")
                } else {
                    let available = !session.isBusy && session.connected
                    GlassEffectContainer(spacing: 6) {
                        HStack(spacing: 7) {
                            control(paused ? "再開" : "一時停止", symbol: paused ? "play.fill" : "pause.fill") {
                                Task { await session.setPaused() }
                            }
                            control("停止", symbol: "stop.fill", tint: recordingTint) {
                                Task { await session.stop() }
                            }
                            .accessibilityLabel("録音を停止")
                        }
                        .opacity(available ? 1 : 0.45)
                        .disabled(!available)
                    }
                }
            }

            if !session.connected {
                Text("録音サービスに再接続中…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if !stopping && session.isBusy {
                loadingStatus("録音操作を反映中")
            } else if capture.phase == .running {
                let name = capture.microphoneName ?? "マイク"
                Label(capture.muted ? "\(name)（ミュート中）" : name, systemImage: capture.muted ? "mic.slash" : "mic")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(name)
                MenuAudioMeter(meter: session.audioMeter, tint: recordingTint)
            }
        }
        .padding(12)
        .background(.primary.opacity(0.045), in: .rect(cornerRadius: 14))
    }

    private func control(_ title: String, symbol: String, tint: Color? = nil,
                         action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: symbol)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(tint ?? Color.primary)
                .padding(.horizontal, 10)
                .frame(height: 36)
                .fixedSize()
                .contentShape(Capsule())
                .glassEffect(tint.map { .regular.tint($0.opacity(0.14)).interactive() } ?? .regular.interactive(),
                             in: .capsule)
        }
        .buttonStyle(.plain)
    }
}

private struct MenuAudioMeter: View {
    @ObservedObject var meter: AudioLevelMeter
    let tint: Color

    var body: some View {
        let level = meter.level
        HStack(spacing: 3) {
            ForEach(0..<20) { index in
                Capsule()
                    .fill(level > Double(index) / 20 ? tint : Color.primary.opacity(0.12))
                    .frame(height: 4)
            }
        }
        .accessibilityHidden(true)
    }
}

@available(macOS 26.0, *)
private struct MenuActivities: View {
    let activities: [BackgroundActivity]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("バックグラウンド処理")
                    .font(.system(size: 12, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 4)
                Text("\(activities.count)件")
                    .font(.caption.monospacedDigit())
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 8)

            ViewThatFits(in: .vertical) {
                rows.fixedSize(horizontal: false, vertical: true)
                SeamScrollView { rows }
                    .scrollBounceBehavior(.basedOnSize)
                    .frame(height: 192)
            }
            .frame(maxHeight: 192, alignment: .top)
        }
    }

    private var rows: some View {
        VStack(spacing: 0) {
            ForEach(activities) { activity in
                BackgroundActivityRow(activity, compact: true)
                    .transition(.opacity)
            }
        }
        .padding(.horizontal, 8)
        .animation(reduceMotion ? nil : SeamMotion.update, value: activities.map(\.id))
    }
}

@available(macOS 26.0, *)
private struct MenuErrorNotice: View {
    let message: String
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.circle")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text(verbatim: message)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(message, forType: .string)
            } label: {
                Image(systemName: "document.on.document")
                    .frame(width: 20, height: 20)
            }
            .help("エラー詳細をコピー")
            .accessibilityLabel("エラー詳細をコピー")
            Button(action: dismiss) {
                Image(systemName: "xmark")
                    .frame(width: 20, height: 20)
            }
            .help("通知を閉じる")
            .accessibilityLabel("通知を閉じる")
        }
        .buttonStyle(.borderless)
        .font(.caption)
        .padding(8)
        .background(.primary.opacity(0.045), in: .rect(cornerRadius: 8))
    }
}

private func loadingStatus(_ message: String) -> some View {
    HStack(spacing: 8) {
        ProgressView().controlSize(.small).accessibilityHidden(true)
        Text(verbatim: message)
            .font(.callout)
            .foregroundStyle(.secondary)
    }
    .padding(.horizontal, 8)
    .padding(.vertical, 4)
}
