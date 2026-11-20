import SwiftUI

public struct BackgroundActivity: Identifiable, Equatable {
    public let id: String
    public let name: String
    public let detail: String?
    public let symbol: String
    public let stage: String?
    public let isRunning: Bool
    public let progress: Double?

    public init(id: String, name: String, detail: String? = nil, symbol: String, progress: Double? = nil,
                stage: String? = nil, isRunning: Bool = true) {
        self.id = id
        self.name = name
        self.detail = detail
        self.symbol = symbol
        self.stage = stage == name || stage == "" ? nil : stage
        self.isRunning = isRunning
        self.progress = progress.flatMap { $0.isFinite ? min(1, max(0, $0)) : nil }
    }
}

@available(macOS 26.0, *)
package struct BackgroundActivityRow: View {
    let activity: BackgroundActivity
    let compact: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    package init(_ activity: BackgroundActivity, compact: Bool = false) {
        self.activity = activity
        self.compact = compact
    }

    package var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: activity.symbol)
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(activity.isRunning ? .primary : .secondary)
                .frame(width: 30, height: 30)
                .background(.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
                .symbolEffect(.pulse, options: .repeating, isActive: activity.isRunning && !reduceMotion)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 5) {
                Text(verbatim: activity.detail ?? activity.name)
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(2)
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if let label = activity.stage ?? (activity.detail == nil ? nil : activity.name) {
                        Text(verbatim: label)
                            .contentTransition(.opacity)
                            .lineLimit(2)
                    }
                    if let progress = activity.progress {
                        Spacer(minLength: 2)
                        Text(progress, format: .percent.precision(.fractionLength(0)))
                            .monospacedDigit()
                            .contentTransition(reduceMotion ? .identity : .numericText())
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                if let progress = activity.progress {
                    ProgressView(value: progress)
                        .controlSize(.small)
                        .tint(.primary)
                        .accessibilityLabel(activity.name)
                }
            }
            .frame(maxWidth: .infinity, minHeight: 30, alignment: .leading)
        }
        .padding(.vertical, compact ? 5 : 8)
        .animation(reduceMotion ? nil : SeamMotion.update, value: activity)
    }
}

/// ツールバーに録音・要約などのバックグラウンド処理をまとめて表示する。
@available(macOS 26.0, *)
public struct ActivityControl<RowMenu: View>: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Stored private var open = false
    private let activities: [BackgroundActivity]
    private let rowMenu: (BackgroundActivity, @escaping () -> Void) -> RowMenu

    public init(activities: [BackgroundActivity],
                @ViewBuilder rowMenu: @escaping (BackgroundActivity, @escaping () -> Void) -> RowMenu) {
        self.activities = activities
        self.rowMenu = rowMenu
    }

    public var body: some View {
        Group {
            if let lead = activities.first {
                activityButton(lead)
                    .transition(.opacity.combined(with: .scale(scale: 0.94, anchor: .trailing)))
            }
        }
        .animation(reduceMotion ? nil : SeamMotion.reposition, value: activities.map(\.id))
    }

    private func activityButton(_ lead: BackgroundActivity) -> some View {
        let extra = activities.count - 1
        let label = lead.stage ?? lead.name
        return Button { open.toggle() } label: {
            HStack(spacing: 8) {
                Image(systemName: lead.symbol)
                    .font(.system(size: 14, weight: .medium))
                    .contentTransition(.symbolEffect(.replace))
                    .symbolEffect(.pulse, options: .repeating, isActive: lead.isRunning && !reduceMotion)
                Text(verbatim: label)
                    .lineLimit(1)
                    .frame(maxWidth: 177, alignment: .leading)
                    .contentTransition(.opacity)
                if let progress = lead.progress {
                    Text(progress, format: .percent.precision(.fractionLength(0)))
                        .fontWeight(.semibold)
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                        .frame(width: 40, alignment: .trailing)
                }
                if extra > 0 {
                    Text("+\(extra)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
            }
            .font(.system(size: 12, weight: .medium))
            .fixedSize(horizontal: true, vertical: false)
            .padding(.horizontal, 12)
            .frame(height: 36)
            .contentShape(Capsule())
            .glassEffect(.regular.interactive(), in: .capsule)
            .animation(reduceMotion ? nil : SeamMotion.update, value: lead.progress)
            .animation(reduceMotion ? nil : SeamMotion.update, value: label)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .help("バックグラウンド処理の詳細")
        .popover(isPresented: $open, arrowEdge: .bottom) {
            SeamScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(activities) { activity in
                        BackgroundActivityRow(activity)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                            .contextMenu { rowMenu(activity, { open = false }) }
                            .transition(.opacity.combined(with: .offset(y: -6)))
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .animation(reduceMotion ? nil : SeamMotion.reposition, value: activities.map(\.id))
            }
            .scrollBounceBehavior(.basedOnSize)
            .scrollEdgeBar(blur: false) {
                Text("バックグラウンド処理")
                    .font(.system(size: 14, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.bottom, 12)
            }
            .frame(maxHeight: 348)
            .fixedSize(horizontal: false, vertical: true)
            .padding(17)
            .frame(minWidth: 275, maxWidth: 350, alignment: .leading)
        }
    }
}

@available(macOS 26.0, *)
public extension ActivityControl where RowMenu == EmptyView {
    init(activities: [BackgroundActivity] = []) {
        self.init(activities: activities) { _, _ in EmptyView() }
    }
}
