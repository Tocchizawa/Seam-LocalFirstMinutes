import AppKit
import SeamUI
import SwiftUI

@available(macOS 26.0, *)
struct ServiceStartupView: View {
    @ObservedObject var service: LocalBackend
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(spacing: 22) {
            Image(nsImage: NSApp.applicationIconImage)
                .resizable()
                .interpolation(.high)
                .frame(width: 76, height: 76)
                .accessibilityHidden(true)

            switch service.state {
            case .preparing(let progress):
                startup(progress)
            case .failed(let message):
                failure(message)
            case .ready:
                EmptyView()
            }
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.seamPaper(for: colorScheme).ignoresSafeArea())
    }

    private func startup(_ progress: LocalBackend.StartupProgress) -> some View {
        VStack(spacing: 24) {
            VStack(spacing: 7) {
                Text(verbatim: progress.title)
                    .font(.system(size: 25, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                    .contentTransition(.opacity)
                if let detail = progress.detail {
                    Text(verbatim: detail)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .transition(.opacity)
                }
            }
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity, minHeight: 57, alignment: .top)
            .animation(reduceMotion ? nil : SeamMotion.update, value: progress)

            phaseTrack(progress.stage)
        }
        .frame(maxWidth: 470)
    }

    private func phaseTrack(_ current: LocalBackend.StartupStage) -> some View {
        let stages = LocalBackend.StartupStage.allCases
        return HStack(alignment: .top, spacing: 6) {
            ForEach(stages, id: \.self) { phase in
                let active = phase == current
                let complete = phase.rawValue < current.rawValue
                VStack(spacing: 9) {
                    trackSegment(active: active, complete: complete)
                    HStack(spacing: 4) {
                        if complete {
                            Image(systemName: "checkmark")
                                .font(.system(size: 8, weight: .semibold))
                                .transition(.opacity)
                        }
                        Text(verbatim: phase.title)
                            .font(.caption2.weight(active ? .semibold : .regular))
                            .lineLimit(1)
                    }
                    .foregroundStyle(active ? .primary : .secondary)
                }
                .frame(maxWidth: .infinity)
            }
        }
        .frame(width: 350)
        .animation(reduceMotion ? nil : SeamMotion.reposition, value: current)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("起動の工程")
        .accessibilityValue("\(current.rawValue + 1) / \(stages.count)、\(current.title)")
    }

    private func trackSegment(active: Bool, complete: Bool) -> some View {
        Capsule()
            .fill(Color.primary.opacity(complete ? 0.78 : (active ? 0.24 : 0.095)))
            .overlay(alignment: .leading) {
                if active && !reduceMotion {
                    GeometryReader { geometry in
                        TimelineView(.animation(minimumInterval: 1.0 / 30)) { context in
                            let period = 1.65
                            let fraction = context.date.timeIntervalSinceReferenceDate
                                .truncatingRemainder(dividingBy: period) / period
                            let width = geometry.size.width * 0.68
                            LinearGradient(colors: [.clear, .primary.opacity(0.74), .clear],
                                           startPoint: .leading, endPoint: .trailing)
                                .frame(width: width)
                                .offset(x: (geometry.size.width + width) * fraction - width)
                        }
                    }
                    .clipShape(Capsule())
                }
            }
            .frame(height: 4)
    }

    private func failure(_ message: String) -> some View {
        VStack(spacing: 10) {
            Text("Seam を起動できませんでした")
                .font(.system(size: 25, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
            Text(message)
                .font(.callout)
                .foregroundStyle(.secondary)
            HStack(spacing: 10) {
                if let log = service.log {
                    Button("ログを開く") { NSWorkspace.shared.open(log) }
                        .buttonStyle(.glass)
                }
                Button("再試行", action: service.start)
                    .buttonStyle(.glassProminent)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.top, 13)
        }
        .multilineTextAlignment(.center)
        .frame(maxWidth: 440)
    }
}
