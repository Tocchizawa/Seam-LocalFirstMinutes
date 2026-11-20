import AppKit
import SeamUI
import SwiftUI

@available(macOS 26.0, *)
struct ServiceStartupView: View {
    @ObservedObject var service: LocalBackend
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme

    private var accent: Color {
        colorScheme == .dark ? Color(red: 0.51, green: 0.81, blue: 0.75) : Color(red: 0.14, green: 0.38, blue: 0.34)
    }

    private var isPreparing: Bool {
        if case .preparing = service.state { return true }
        return false
    }

    var body: some View {
        VStack(spacing: 22) {
            startupMark
            Text(isPreparing ? "Seam を起動中" : "Seam")
                .font(.system(size: 28, weight: .semibold))
                .accessibilityAddTraits(.isHeader)

            switch service.state {
            case .preparing(let progress):
                stages(progress)
            case .failed(let message):
                failure(message)
            case .ready:
                EmptyView()
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background {
            Color.seamPaper(for: colorScheme)
                .overlay {
                    RadialGradient(colors: [accent.opacity(colorScheme == .dark ? 0.12 : 0.07), .clear],
                                   center: UnitPoint(x: 0.5, y: 0.34), startRadius: 0, endRadius: 310)
                }
                .ignoresSafeArea()
        }
    }

    private var startupMark: some View {
        ZStack {
            if isPreparing {
                Circle().strokeBorder(.primary.opacity(0.08), lineWidth: 1)
                TimelineView(.animation(minimumInterval: 1.0 / 30, paused: reduceMotion)) { context in
                    let period = 2.6
                    let arcLength = 0.28
                    let elapsed = context.date.timeIntervalSinceReferenceDate
                    let turn = reduceMotion ? 0 : elapsed.truncatingRemainder(dividingBy: period) / period
                    Circle()
                        .trim(from: 0, to: arcLength)
                        .stroke(
                            AngularGradient(colors: [accent.opacity(0.08), accent], center: .center,
                                            startAngle: .zero, endAngle: .degrees(arcLength * 360)),
                            style: StrokeStyle(lineWidth: 2, lineCap: .round)
                        )
                        .rotationEffect(.degrees(turn * 360 - 90))
                }
            }
            Image(nsImage: NSApp.applicationIconImage)
                .resizable()
                .interpolation(.high)
                .frame(width: 82, height: 82)
        }
        .frame(width: 116, height: 116)
        .accessibilityHidden(true)
    }

    private func stages(_ progress: LocalBackend.StartupProgress) -> some View {
        VStack(spacing: 3) {
            ForEach(LocalBackend.StartupStage.allCases, id: \.self) { stage in
                stageRow(stage, progress: progress)
            }
        }
        .padding(8)
        .frame(width: 402)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
        .animation(reduceMotion ? nil : .smooth(duration: 0.32), value: progress.stage)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("起動の進捗")
    }

    private func stageRow(_ stage: LocalBackend.StartupStage, progress: LocalBackend.StartupProgress) -> some View {
        let active = stage == progress.stage
        let completed = stage.rawValue < progress.stage.rawValue
        return HStack(spacing: 12) {
            Image(systemName: completed ? "checkmark" : stage.symbol)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(active || completed ? accent : .secondary)
                .frame(width: 32, height: 32)
                .background(active || completed ? accent.opacity(0.12) : Color.primary.opacity(0.04),
                            in: .rect(cornerRadius: 9))
                .contentTransition(reduceMotion ? .identity : .symbolEffect(.replace))
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 3) {
                Text(stage.title)
                    .font(.callout.weight(active ? .semibold : .medium))
                    .foregroundStyle(active || completed ? .primary : .secondary)
                if active {
                    Text(progress.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .contentTransition(.opacity)
                        .animation(reduceMotion ? nil : SeamMotion.update, value: progress.detail)
                        .transition(.opacity)
                }
            }
            Spacer(minLength: 8)
            if active {
                Text("進行中")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(accent)
                    .transition(.opacity)
                    .accessibilityHidden(true)
            }
        }
        .padding(.vertical, 7)
        .padding(.horizontal, 9)
        .background(active ? accent.opacity(0.08) : .clear, in: .rect(cornerRadius: 14))
        .accessibilityElement(children: .combine)
        .accessibilityValue(completed ? "完了" : (active ? "進行中" : "待機中"))
    }

    private func failure(_ message: String) -> some View {
        VStack(spacing: 10) {
            Label("録音サービスを起動できませんでした", systemImage: "exclamationmark.circle")
                .font(.headline)
            Text(message).foregroundStyle(.secondary).multilineTextAlignment(.center)
            HStack {
                if let log = service.log {
                    Button("ログを開く") { NSWorkspace.shared.open(log) }
                        .buttonStyle(.glass)
                }
                Button("再試行", action: service.start)
                    .buttonStyle(.glassProminent)
            }
            .padding(.top, 4)
        }
        .padding(18)
        .frame(width: 402)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}
