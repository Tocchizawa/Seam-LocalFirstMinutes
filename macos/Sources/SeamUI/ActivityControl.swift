import SwiftUI

public struct BackgroundActivity: Identifiable {
    public let id: String
    public let name: String
    public let detail: String?
    public let symbol: String
    public let progress: Double?

    public init(id: String, name: String, detail: String? = nil, symbol: String, progress: Double? = nil) {
        self.id = id
        self.name = name
        self.detail = detail
        self.symbol = symbol
        self.progress = progress.flatMap { $0.isFinite ? min(1, max(0, $0)) : nil }
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
        if let lead = activities.first {
            let extra = activities.count - 1
            Button { open.toggle() } label: {
                HStack(spacing: 8) {
                    Image(systemName: lead.symbol)
                        .font(.system(size: 14, weight: .medium))
                        .contentTransition(.symbolEffect(.replace))
                    Text(verbatim: lead.name)
                        .lineLimit(1)
                        .frame(maxWidth: 177, alignment: .leading)
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
            }
            .buttonStyle(.plain)
            .accessibilityLabel(lead.name)
            .help("バックグラウンド処理の詳細")
            .popover(isPresented: $open, arrowEdge: .bottom) {
                SeamScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(activities) { activity in
                            row(activity)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.vertical, 5)
                                .contentShape(Rectangle())
                                .contextMenu { rowMenu(activity, { open = false }) }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
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

    private func row(_ activity: BackgroundActivity) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: activity.symbol)
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(.secondary)
                .frame(width: 19)
            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text(verbatim: activity.name).font(.system(size: 13, weight: .semibold))
                    if let progress = activity.progress {
                        Spacer()
                        Text(progress, format: .percent.precision(.fractionLength(0)))
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
                if let detail = activity.detail {
                    Text(verbatim: detail).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let progress = activity.progress {
                    ProgressView(value: progress).accessibilityLabel(activity.name)
                }
            }
        }
    }
}

@available(macOS 26.0, *)
public extension ActivityControl where RowMenu == EmptyView {
    init(activities: [BackgroundActivity] = []) {
        self.init(activities: activities) { _, _ in EmptyView() }
    }
}
