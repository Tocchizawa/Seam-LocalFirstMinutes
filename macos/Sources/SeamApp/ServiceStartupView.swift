import AppKit
import SeamUI
import SwiftUI

@available(macOS 26.0, *)
struct ServiceStartupView: View {
    @ObservedObject var service: LocalBackend
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(spacing: 18) {
            Image(nsImage: NSApp.applicationIconImage)
                .resizable()
                .interpolation(.high)
                .frame(width: 84, height: 84)
                .accessibilityHidden(true)
            Text("Seam")
                .font(.system(size: 30, weight: .semibold))
                .accessibilityAddTraits(.isHeader)

            switch service.state {
            case .preparing(let step):
                VStack(spacing: 8) {
                    HStack(spacing: 10) {
                        Text(step.message)
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                        Text(step.progress.formatted(.percent.precision(.fractionLength(0))))
                            .monospacedDigit()
                            .contentTransition(reduceMotion ? .identity : .numericText())
                    }
                    .font(.callout)
                    ProgressView(value: step.progress)
                        .progressViewStyle(.linear)
                        .accessibilityLabel("起動の進捗")
                }
                .frame(width: 310)
                .animation(reduceMotion ? nil : SeamMotion.update, value: step)
            case .failed(let message):
                VStack(spacing: 10) {
                    Text("録音サービスを起動できませんでした").font(.headline)
                    Text(message).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    HStack {
                        if let log = service.log {
                            Button("ログを開く") { NSWorkspace.shared.open(log) }
                        }
                        Button("再試行") { service.start() }
                            .buttonStyle(.borderedProminent)
                    }
                }
                .frame(maxWidth: 380)
            case .ready:
                EmptyView()
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.seamPaper(for: colorScheme).ignoresSafeArea())
    }
}
