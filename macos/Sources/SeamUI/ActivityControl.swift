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

@available(macOS 26.0, *)
public struct ActivityControl: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @ObservedObject private var downloads: WhisperDownloads
    @Stored private var open = false
    private let activities: [BackgroundActivity]
    private let openModels: () -> Void

    public init(downloads: WhisperDownloads, activities: [BackgroundActivity] = [], openModels: @escaping () -> Void) {
        self.downloads = downloads
        self.activities = activities
        self.openModels = openModels
    }

    public var body: some View {
        if let lead = modelActivity ?? activities.first {
            let extra = activities.count - (modelActivity == nil ? 1 : 0)
            Button {
                open.toggle()
            } label: {
                HStack(spacing: 8) {
                    activityIcon(lead)
                    Text(verbatim: lead.id == "model-download" ? "\(lead.name) を取得中" : lead.name)
                        .lineLimit(1)
                        .frame(maxWidth: 177, alignment: .leading)
                    if let progress = lead.progress {
                        Text(progress, format: .percent.precision(.fractionLength(0)))
                            .fontWeight(.semibold)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .fixedSize(horizontal: true, vertical: false)
                            .frame(width: 40, alignment: .trailing)
                            .contentTransition(.numericText())
                    }
                    if extra > 0 {
                        Text("+\(extra)")
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
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
            .accessibilityLabel(accessibleName(lead))
            .help("バックグラウンド処理の詳細")
            .popover(isPresented: $open, arrowEdge: .bottom) {
                popover
            }
        }
    }

    private func activityIcon(_ activity: BackgroundActivity) -> some View {
        ZStack {
            if activity.id == "model-download", let progress = activity.progress {
                let tint = colorScheme == .dark ? Color(red: 0.51, green: 0.81, blue: 0.75)
                    : Color(red: 0.14, green: 0.38, blue: 0.34)
                Circle().strokeBorder(.primary.opacity(0.16), lineWidth: 1.5)
                Circle()
                    .trim(from: 0, to: progress)
                    .stroke(tint, style: StrokeStyle(lineWidth: 1.5, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                    .padding(0.75)
                Image(systemName: "arrow.down")
                    .font(.system(size: 8, weight: .semibold))
                    .foregroundStyle(tint)
            } else {
                Image(systemName: activity.symbol)
                    .font(.system(size: 14, weight: .medium))
                    .contentTransition(.symbolEffect(.replace))
            }
        }
        .frame(width: 17, height: 17)
        .accessibilityHidden(true)
    }

    private var modelActivity: BackgroundActivity? {
        if let current = downloads.currentModel {
            let name = downloads.label(current)
            let detail: String
            if downloads.isPreview {
                detail = "試作用のダウンロード"
            } else if let status = downloads.status, let total = status.totalBytes, total > 0 {
                detail = "\(bytes(status.currentBytes ?? 0)) / \(bytes(total))"
            } else {
                detail = "文字起こしモデルをダウンロード中"
            }
            return .init(id: "model-download", name: name, detail: detail, symbol: "arrow.down.circle", progress: downloads.fraction)
        }
        if let failure = downloads.failure {
            return .init(id: "model-failure", name: "ダウンロードに失敗", detail: failure, symbol: "exclamationmark.circle")
        }
        if let completed = downloads.completed {
            return .init(id: "model-ready", name: "ダウンロード完了", detail: downloads.label(completed), symbol: "checkmark.circle")
        }
        return nil
    }

    private var popover: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let model = modelActivity {
                    row(model)
                    HStack {
                        Button("文字起こしモデルを開く") {
                            open = false
                            openModels()
                        }
                        if downloads.failure != nil {
                            Spacer()
                            Button("閉じる") {
                                downloads.dismissFailure()
                                open = false
                            }
                        }
                    }
                    if !activities.isEmpty { Divider() }
                }
                ForEach(activities) { row($0) }
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

    private func row(_ activity: BackgroundActivity) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: activity.symbol)
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(.secondary)
                .frame(width: 19)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text(verbatim: activity.name)
                        .font(.system(size: 13, weight: .semibold))
                    if let fraction = activity.progress {
                        Spacer()
                        Text(fraction, format: .percent.precision(.fractionLength(0)))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
                if let detail = activity.detail {
                    Text(verbatim: detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let fraction = activity.progress {
                    ProgressView(value: fraction)
                        .accessibilityLabel(activity.name)
                } else if activity.id == "model-download" {
                    ProgressView().controlSize(.small).accessibilityLabel("ダウンロードを準備中")
                }
            }
        }
    }

    private func accessibleName(_ activity: BackgroundActivity) -> String {
        let title = activity.id == "model-download" ? "\(activity.name)をダウンロード中" : activity.name
        if let progress = activity.progress {
            return title + " " + progress.formatted(.percent.precision(.fractionLength(0)))
        }
        return title
    }

    private func bytes(_ count: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(count), countStyle: .file)
    }
}
