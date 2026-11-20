import AppKit
import SeamUI
import SeamWorkspace
import SwiftUI

private enum SeamMenuMark {
    // The menu bar uses the source image's point size, even if its SwiftUI label is resized.
    static let image: NSImage = {
        let artwork = Bundle.main.url(forResource: "SeamMenuBar", withExtension: "svg")
            .flatMap(NSImage.init(contentsOf:))
        let image = artwork ?? NSImage(systemSymbolName: "waveform", accessibilityDescription: nil) ?? NSImage()
        image.size = NSSize(width: 18, height: 18)
        image.isTemplate = true
        return image
    }()

    static var label: some View {
        Image(nsImage: image).accessibilityLabel("Seam")
    }
}

private extension WorkspaceSession.Capture.Phase {
    var menuStatus: String {
        switch self {
        case .starting: "録音を開始中"
        case .running: "録音中"
        case .paused: "一時停止中"
        case .stopping: "録音を保存中"
        }
    }

    var menuSymbol: String {
        switch self {
        case .starting, .running: "record.circle.fill"
        case .paused: "pause.circle.fill"
        case .stopping: "square.circle.fill"
        }
    }
}

struct RecordingMenuBarLabel: View {
    @ObservedObject var service: LocalBackend

    var body: some View {
        if let session = service.session {
            LiveRecordingMenuLabel(session: session)
        } else {
            SeamMenuMark.label
        }
    }
}

private struct LiveRecordingMenuLabel: View {
    @ObservedObject var session: WorkspaceSession

    var body: some View {
        if let capture = session.capture, capture.phase != .starting {
            let time = minutesTime(capture.elapsed)
            Label {
                Text(time).monospacedDigit()
            } icon: {
                Image(systemName: capture.phase.menuSymbol)
            }
            .labelStyle(.titleAndIcon)
            .accessibilityLabel("\(capture.phase.menuStatus) \(time)")
        } else {
            SeamMenuMark.label
        }
    }
}

@available(macOS 26.0, *)
struct RecordingMenuPanel: View {
    @ObservedObject var service: LocalBackend
    @Environment(\.openWindow) private var openWindow
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let session = service.session {
                RecordingMenuContent(session: session)
            } else {
                switch service.state {
                case .failed:
                    RecordingMenuStatus(message: "録音サービスを起動できませんでした")
                case .preparing, .ready:
                    RecordingMenuStatus(message: "録音サービスを準備中")
                }
            }

            Divider()
                .padding(.horizontal, 8)

            Button {
                NSApp.activate(ignoringOtherApps: true)
                openWindow(id: "workspace")
            } label: {
                HStack(spacing: 10) {
                    Image(systemName: "macwindow")
                        .frame(width: 18)
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                    Text("Seamを開く")
                        .fontWeight(.medium)
                    Spacer(minLength: 8)
                    Image(systemName: "arrow.up.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }
                .font(.system(size: 13))
                .frame(height: 36)
                .padding(.horizontal, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
        }
        .padding(12)
        .frame(width: 284)
    }
}

private struct RecordingMenuStatus: View {
    let message: String

    var body: some View {
        HStack(spacing: 12) {
            Image(nsImage: SeamMenuMark.image)
                .frame(width: 32, height: 32)
                .background(.quaternary.opacity(0.55), in: .rect(cornerRadius: 8))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text("Seam")
                    .font(.system(size: 14, weight: .semibold))
                Text(message)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(8)
    }
}

@available(macOS 26.0, *)
private struct RecordingMenuContent: View {
    @ObservedObject var session: WorkspaceSession
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var red: Color {
        colorScheme == .dark ? Color(red: 1, green: 0.52, blue: 0.56) : Color(red: 0.68, green: 0.12, blue: 0.20)
    }

    var body: some View {
        if let capture = session.capture, capture.phase != .starting {
            let paused = capture.phase == .paused
            let stopping = capture.phase == .stopping
            let elapsed = minutesTime(capture.elapsed)

            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 6) {
                        Label {
                            Text(capture.phase.menuStatus)
                        } icon: {
                            Circle()
                                .fill(paused || stopping ? Color.secondary : red)
                                .frame(width: 6, height: 6)
                        }
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.secondary)

                        Text(elapsed)
                            .font(.system(size: 28, weight: .medium))
                            .monospacedDigit()
                            .lineLimit(1)
                            .minimumScaleFactor(0.8)
                            .contentTransition(reduceMotion ? .identity : .numericText())
                            .animation(reduceMotion ? nil : SeamMotion.feedback, value: elapsed)
                            .accessibilityLabel("録音時間")
                            .accessibilityValue(elapsed)
                    }
                    Spacer(minLength: 4)
                    GlassEffectContainer(spacing: 6) {
                        HStack(spacing: 8) {
                            control(paused ? "play.fill" : "pause.fill", label: paused ? "再開" : "一時停止") {
                                Task { await session.setPaused() }
                            }
                            control("stop.fill", label: "録音を停止", destructive: true) {
                                Task { await session.stop() }
                            }
                        }
                        .opacity(stopping ? 0.45 : 1)
                        .disabled(stopping)
                    }
                }

                microphoneInput(capture)
                audioMeter(level: capture.phase == .running ? session.audioLevel : 0)
            }
            .padding(8)
        } else {
            RecordingMenuStatus(message: session.capture?.phase.menuStatus ?? "録音していません")
        }
        if let error = session.error {
            Label(error, systemImage: "exclamationmark.circle")
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .padding(.horizontal, 8)
        }
    }

    private func microphoneInput(_ capture: WorkspaceSession.Capture) -> some View {
        let name = capture.microphoneName ?? "マイク"
        let message = capture.muted ? "\(name)（ミュート中）" : name
        return Label(message, systemImage: capture.muted ? "mic.slash" : "mic")
            .font(.system(size: 12))
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .truncationMode(.middle)
            .help(message)
    }

    private func audioMeter(level: Double) -> some View {
        let segmentCount = 20
        return HStack(spacing: 3) {
            ForEach(0..<segmentCount, id: \.self) { index in
                Capsule()
                    .fill(level > Double(index) / Double(segmentCount) ? red : Color.primary.opacity(0.12))
                    .frame(height: 4)
            }
        }
        .accessibilityRepresentation {
            ProgressView(value: level)
                .accessibilityLabel("録音レベル")
        }
    }

    private func control(_ symbol: String, label: String, destructive: Bool = false,
                         action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 13, weight: .semibold))
                .frame(width: 40, height: 40)
                .foregroundStyle(destructive ? red : Color.primary)
                .contentShape(Circle())
                .glassEffect(destructive ? .regular.tint(red.opacity(0.14)).interactive() : .regular.interactive(),
                             in: .circle)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .help(label)
    }
}
