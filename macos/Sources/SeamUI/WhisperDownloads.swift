import SwiftUI

#if SWIFT_PACKAGE
import SeamCore
#endif

/// Whisperモデルの取得状態を管理する。画面側のモデル一覧は下の WhisperModelRows。
@MainActor
public final class WhisperDownloads: ObservableObject {
    @Published public private(set) var models: [WhisperModels.Model]
    @Published public private(set) var status: WhisperModels.Download?
    @Published public private(set) var requesting: String?
    @Published public private(set) var error: String?
    @Published public private(set) var catalogError: String?
    @Published public private(set) var completed: String?

    private let api: SeamAPI?
    private var revision = 0
    private var fetching = false
    private var dismissedFailure: WhisperModels.Download?
    private var completionTask: Task<Void, Never>?

    public init(api: SeamAPI?) {
        self.api = api
        models = api == nil ? Self.sampleModels : []
    }

    public var isPreview: Bool { api == nil }
    public var currentModel: String? {
        requesting ?? (status?.state == "downloading" ? status?.model : nil)
    }
    public var fraction: Double? {
        guard requesting == nil, let percent = status?.percent, percent.isFinite else { return nil }
        return min(1, max(0, percent / 100))
    }
    public var failure: String? {
        error ?? (status?.state == "error" && status != dismissedFailure ? status?.error : nil)
    }
    public var active: Bool { currentModel != nil }

    public func label(_ name: String) -> String { models.first { $0.name == name }?.label ?? name }

    public func monitor() async {
        guard api != nil else { return }
        while !Task.isCancelled {
            await refresh()
            do { try await Task.sleep(for: .seconds(active ? 0.8 : 3)) }
            catch { return }
        }
    }

    public func refresh() async {
        guard let api, requesting == nil, !fetching else { return }
        fetching = true
        defer { fetching = false }
        let before = revision
        do {
            let result = try await api.whisperModels()
            guard !Task.isCancelled, requesting == nil, revision == before else { return }
            if catalogError != nil { catalogError = nil }
            if models != result.models { models = result.models }
            update(result.download)
        } catch is CancellationError {
        } catch {
            if models.isEmpty && !Task.isCancelled && catalogError != error.localizedDescription {
                catalogError = error.localizedDescription
            }
        }
    }

    public func start(_ model: String) async {
        guard !active, models.contains(where: { $0.name == model && !$0.downloaded }) else { return }
        revision += 1
        error = nil
        completed = nil
        dismissedFailure = nil
        requesting = model
        if let api {
            do {
                let result = try await api.downloadWhisper(model)
                update(result)
            } catch {
                self.error = error.localizedDescription
            }
            requesting = nil
            await refresh()
        } else {
            requesting = nil
            await previewDownload(model)
        }
    }

    public func remove(_ model: String) async throws {
        if let api {
            try await api.removeWhisper(model)
            revision += 1
            await refresh()
        } else if let index = models.firstIndex(where: { $0.name == model }) {
            let entry = models[index]
            models[index] = .init(name: entry.name, label: entry.label, state: "not_downloaded", downloaded: false)
        }
    }

    public func dismissFailure() {
        error = nil
        dismissedFailure = status
    }

    private func update(_ next: WhisperModels.Download) {
        guard status != next else { return }
        let justFinished = status?.state == "downloading" && status?.model == next.model && next.state == "ready"
        status = next
        if next.state == "downloading", error != nil { error = nil }
        if justFinished, let name = next.model {
            completed = name
            completionTask?.cancel()
            completionTask = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(5)) }
                catch { return }
                self?.completed = nil
            }
        }
    }

    private func previewDownload(_ model: String) async {
        for frame in 0...50 {
            update(.init(state: "downloading", model: model, percent: Double(frame) * 2))
            do { try await Task.sleep(for: .milliseconds(240)) }
            catch { return }
        }
        if let index = models.firstIndex(where: { $0.name == model }) {
            let entry = models[index]
            models[index] = .init(name: entry.name, label: entry.label, state: "downloaded", downloaded: true)
        }
        update(.init(state: "ready", model: model, percent: 100))
    }

    private static let sampleModels: [WhisperModels.Model] = [
        .init(name: "tiny", label: "Tiny", state: "not_downloaded", downloaded: false),
        .init(name: "base", label: "Base", state: "not_downloaded", downloaded: false),
        .init(name: "small", label: "Small", state: "not_downloaded", downloaded: false),
        .init(name: "medium", label: "Medium", state: "downloaded", downloaded: true),
        .init(name: "large-v1", label: "Large v1", state: "not_downloaded", downloaded: false),
        .init(name: "large-v2", label: "Large v2", state: "not_downloaded", downloaded: false),
        .init(name: "large-v3", label: "Large v3", state: "not_downloaded", downloaded: false),
        .init(name: "large-v3-turbo", label: "Large v3 Turbo", state: "not_downloaded", downloaded: false),
    ]
}

@available(macOS 15.0, *)
struct WhisperModelRows: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var downloads: WhisperDownloads
    @Binding var selection: String
    let compact: Bool
    let tint: Color
    let remove: (WhisperModels.Model) -> Void

    private let featuredIDs = ["small", "medium", "large-v3-turbo"]
    private var motion: Animation? { reduceMotion ? nil : SeamMotion.update }

    var body: some View {
        // おすすめモデルと各モデルの一覧を組み立てる。
        if downloads.models.isEmpty {
            SettingsRow {
                if let error = downloads.catalogError {
                    Text(error).foregroundStyle(.secondary)
                    Spacer()
                    Button("再読み込み") { Task { await downloads.refresh() } }
                        .buttonStyle(SettingsActionStyle())
                } else {
                    ProgressView().controlSize(.small).accessibilityLabel("モデルを読み込み中")
                }
            }
        } else {
            let featured = downloads.models.filter { featuredIDs.contains($0.name) }
            let other = downloads.models.filter { !featuredIDs.contains($0.name) }

            SettingsRows {
                if !downloads.models.contains(where: { $0.name == selection }) {
                    SettingsRow("選択中のモデル") {
                        Text(selection).font(.callout).foregroundStyle(.secondary)
                    }
                }
                ForEach(featured) { row($0) }
                if !other.isEmpty {
                    SettingsDisclosure(title: "その他のモデル",
                                       initiallyExpanded: other.contains { $0.name == selection }) {
                        ForEach(other) { row($0) }
                    }
                }
            }
        }
    }

    private func row(_ entry: WhisperModels.Model) -> some View {
        let selected = selection == entry.name
        return HStack(spacing: 0) {
            Button {
                withAnimation(motion) { selection = entry.name }
            } label: {
                HStack(spacing: 12) {
                    Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 16, weight: .medium))
                        .foregroundStyle(selected ? tint : .secondary)
                        .frame(width: 20)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(verbatim: entry.label)
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(.primary)
                        if let note = note(for: entry) {
                            Text(verbatim: note).font(.system(size: 11)).foregroundStyle(.secondary)
                        }
                    }
                    .lineLimit(1)
                    Spacer(minLength: 0)
                }
                .padding(.leading, 16)
                .padding(.trailing, 8)
                .frame(maxWidth: .infinity, minHeight: 56, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(ReaderHoverStyle(tint: .primary, motion: motion))
            .accessibilityLabel(entry.label)
            .accessibilityAddTraits(selected ? .isSelected : [])
            .help(selected ? "\(entry.label)を選択中" : "\(entry.label)を使う")

            actions(for: entry)
                .frame(width: compact ? 100 : 164, alignment: .trailing)
                .padding(.trailing, 12)
        }
        .frame(maxWidth: .infinity, minHeight: 56)
    }

    @ViewBuilder
    private func actions(for entry: WhisperModels.Model) -> some View {
        if downloads.currentModel == entry.name {
            HStack(spacing: 9) {
                if let fraction = downloads.fraction {
                    ProgressView(value: fraction)
                        .tint(tint)
                        .animation(motion, value: fraction)
                        .accessibilityLabel("\(entry.label)のダウンロード進捗")
                    Text(fraction, format: .percent.precision(.fractionLength(0)))
                        .font(.system(size: 11, weight: .medium, design: .monospaced))
                        .monospacedDigit()
                        .contentTransition(.numericText())
                        .fixedSize()
                } else {
                    ProgressView().controlSize(.small)
                    Text("準備中").font(.system(size: 11)).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 4)
        } else if entry.downloaded {
            HStack(spacing: 4) {
                Text("取得済み")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .fixedSize()
                Menu {
                    Button("ダウンロードを削除", role: .destructive) { remove(entry) }
                        .disabled(entry.state == "loaded")
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.system(size: 14, weight: .medium))
                        .frame(width: 36, height: 36)
                        .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8))
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .accessibilityLabel("\(entry.label)の操作")
                .help("\(entry.label)の操作")
            }
        } else {
            Button {
                Task { await downloads.start(entry.name) }
            } label: {
                if compact {
                    Image(systemName: "arrow.down.to.line")
                } else {
                    Text("ダウンロード")
                }
            }
            .buttonStyle(SettingsActionStyle())
            .disabled(downloads.active)
            .accessibilityLabel("\(entry.label)をダウンロード")
            .help("\(entry.label)をダウンロード")
        }
    }

    private func note(for model: WhisperModels.Model) -> String? {
        let note: String? = switch model.name {
        case "tiny", "base": "軽量モデル"
        case "small": "処理速度を優先"
        case "medium": "速度と精度のバランス"
        case "large-v3": "精度を優先"
        case "large-v3-turbo": "Large v3を高速化"
        default: nil
        }
        let size = model.sizeBytes > 0 ? ByteCountFormatter.string(fromByteCount: Int64(model.sizeBytes), countStyle: .file) : nil
        let description = [note, size].compactMap { $0 }.joined(separator: "  ·  ")
        return description.isEmpty ? nil : description
    }
}
