import SeamUI
import SwiftUI

@available(macOS 26.0, *)
struct ArchiveListRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme

    let title: Text
    let date: String?
    let duration: Double?
    let preview: Text?
    let activity: BackgroundActivity?
    let problem: String?
    let opensDetail: Bool

    private var tint: Color {
        colorScheme == .dark ? Color(red: 0.51, green: 0.81, blue: 0.75) : Color(red: 0.14, green: 0.38, blue: 0.34)
    }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: opensDetail ? "doc.text.fill" : "waveform")
                .font(.system(size: 17, weight: .medium))
                .foregroundStyle(tint)
                .frame(width: 38, height: 38)
                .background(tint.opacity(0.11), in: .rect(cornerRadius: 10))
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 4) {
                title
                    .font(.system(size: 15, weight: .semibold))
                    .lineLimit(2)
                if let date {
                    Text(verbatim: date)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                if let preview {
                    preview
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                if let activity { activityLabel(activity) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            if let duration, duration.isFinite, duration > 0 {
                let time = minutesTime(duration)
                Label(time, systemImage: "waveform")
                    .font(.caption.weight(.medium))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .background(.primary.opacity(0.055), in: .capsule)
                    .fixedSize()
                    .accessibilityLabel("録音時間 \(time)")
                    .help("録音時間")
            }
            if opensDetail {
                Image(systemName: "chevron.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(minHeight: 64)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    private func activityLabel(_ activity: BackgroundActivity) -> some View {
        let failed = activity.symbol == "exclamationmark.circle"
        let label = activity.stage ?? activity.name
        return HStack(spacing: 5) {
            Image(systemName: activity.symbol)
                .symbolEffect(.pulse, options: .repeating, isActive: activity.isRunning && !reduceMotion)
                .accessibilityHidden(true)
            Text(verbatim: label).lineLimit(1)
            if let progress = activity.progress {
                Text(progress, format: .percent.precision(.fractionLength(0)))
                    .monospacedDigit()
            }
        }
        .font(.caption.weight(.medium))
        .foregroundStyle(failed ? Color.orange : tint)
        .help(problem ?? label)
    }
}
