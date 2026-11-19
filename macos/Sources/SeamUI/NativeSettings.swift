import SwiftUI
#if SWIFT_PACKAGE
import SeamCore
#endif

@MainActor
private final class SettingsEditor: ObservableObject {
    @Published private(set) var document = SettingsDocument()
    @Published private(set) var changes = SettingsDocument()
    @Published private(set) var saving = false
    @Published private(set) var loading = true
    @Published var feedback: String?

    let api: SeamAPI?
    private var baseline = SettingsDocument()
    private let previewKey = "seam-preview-settings"

    init(api: SeamAPI?) { self.api = api }

    func load() async {
        defer { loading = false }
        do {
            if let api { baseline = try await api.settings() }
            else if let stored = UserDefaults.standard.data(forKey: previewKey) {
                baseline = try JSONDecoder().decode(SettingsDocument.self, from: stored)
            }
            document = baseline
            changes = SettingsDocument()
        } catch {
            feedback = error.localizedDescription
        }
    }

    func set(_ path: String, _ value: SettingValue) {
        document[path] = value
        changes[path] = value == baseline[path] ? nil : value
        feedback = nil
    }

    func save() async {
        guard !saving, !changes.isEmpty else { return }
        saving = true
        defer { saving = false }
        do {
            if let api { document = try await api.saveSettings(changes) }
            else { UserDefaults.standard.set(try JSONEncoder().encode(document), forKey: previewKey) }
            baseline = document
            changes = SettingsDocument()
            feedback = "保存しました"
        } catch {
            feedback = error.localizedDescription
        }
    }
}

public enum NativeSettingsPage: String, CaseIterable, Identifiable {
    case input = "録音", transcription = "文字起こし", summary = "要約", speakers = "話者", appearance = "外観", advanced = "詳細"
    public var id: Self { self }
}

private enum SummaryProvider: String, CaseIterable, Identifiable {
    case ollama, claude_api, openai, gemini, claude_code, codex
    var id: Self { self }

    var label: String {
        switch self {
        case .ollama: "Ollama"
        case .claude_api: "Claude API"
        case .openai: "OpenAI API"
        case .gemini: "Gemini"
        case .claude_code: "Claude Code"
        case .codex: "Codex"
        }
    }

    var modelHint: String {
        switch self {
        case .ollama: "qwen3:8b"
        case .claude_api: "claude-sonnet-4-6"
        case .openai: "gpt-4o-mini"
        case .gemini: "gemini-2.0-flash"
        case .claude_code: "sonnet"
        case .codex: ""
        }
    }

    var isCLI: Bool { self == .claude_code || self == .codex }
    var requiresKey: Bool { self == .claude_api || self == .openai || self == .gemini }
}

/// An embedded settings panel. App preferences save only the edited fields
/// through the original backend API, leaving unrelated config values intact.
@available(macOS 15.0, *)
public struct NativeSettings<Appearance: View>: View {
    private typealias Stored<Value> = SwiftUI.State<Value>

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @StateObject private var editor: SettingsEditor
    @ObservedObject private var downloads: WhisperDownloads
    @Binding private var page: NativeSettingsPage
    @Stored private var devices: [AudioDevice] = []
    @Stored private var cliOptions: [CLIModel] = []
    @Stored private var hasProviderKey = false
    @Stored private var checkingProvider = false
    @Stored private var connectionResult: ServiceConnectionCheck?
    @Stored private var connectionError: String?
    @Stored private var hasDiarizationToken = false
    @Stored private var key = ""
    @Stored private var diarizationToken = ""
    @Stored private var profiles: [SpeakerProfile] = []
    @Stored private var profileNames: [String: String] = [:]
    @Stored private var profilesToMerge: Set<String> = []
    @Stored private var confirmation: Removal?
    @Stored private var confirmingCloud = false
    @Stored private var busy = false
    @StateObject private var speakerAudio = MinutesPlayback()
    @Stored private var speakerPlaying: String?

    private let appearance: () -> Appearance
    private let compact: Bool

    private struct Removal: Identifiable {
        enum Target { case model, speaker }
        let target: Target
        let id: String
        let label: String
    }

    private struct ConnectionTarget: Equatable {
        let provider: SummaryProvider
        let model: String?
        let effort: String?
    }

    public init(api: SeamAPI?, downloads: WhisperDownloads, page: Binding<NativeSettingsPage>, compact: Bool = false,
                @ViewBuilder appearance: @escaping () -> Appearance) {
        _editor = StateObject(wrappedValue: SettingsEditor(api: api))
        self.downloads = downloads
        self._page = page
        self.compact = compact
        self.appearance = appearance
    }

    private var motion: Animation? { reduceMotion ? nil : SeamMotion.feedback }
    private var accent: Color {
        colorScheme == .dark ? Color(red: 0.51, green: 0.81, blue: 0.75) : Color(red: 0.14, green: 0.38, blue: 0.34)
    }

    private var provider: SummaryProvider {
        SummaryProvider(rawValue: editor.document["minutes_ai.provider"]?.text ?? "") ?? .ollama
    }

    private var connectionTarget: ConnectionTarget {
        let path = "minutes_ai." + provider.rawValue
        return ConnectionTarget(provider: provider,
                                model: editor.document[path + ".model"]?.text,
                                effort: provider == .codex ? editor.document[path + ".reasoning_effort"]?.text : nil)
    }

    private var gutter: CGFloat { compact ? 16 : 32 }

    public var body: some View {
        ZStack(alignment: .topLeading) {
            settingsContent
                .id(page)
                .transition(.identity)
        }
        .animatePageChange(for: page, enabled: !reduceMotion)
        .scrollEdgeBar { settingsHeader }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .tint(.primary)
        .task { await editor.load() }
        .task(id: page) { await refreshPage() }
        .task(id: provider) { await refreshProvider() }
        .onChange(of: connectionTarget) { _, _ in
            connectionResult = nil
            connectionError = nil
        }
        .onDisappear { speakerAudio.clear() }
        .alert(item: $confirmation) { item in
            Alert(title: Text("\(item.label)を削除しますか"),
                  primaryButton: .destructive(Text("削除")) { remove(item) },
                  secondaryButton: .cancel(Text("キャンセル")))
        }
        .alert("クラウドで要約しますか", isPresented: $confirmingCloud) {
            Button("許可する") {
                let selectedProvider = provider
                act { api in try await api.allowSummaryProvider(selectedProvider.rawValue) }
            }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text("文字起こしの本文が\(provider.label)に送信されます。")
        }
    }

    private var settingsContent: some View {
        SeamScrollView {
            Group(sections: fields) { sections in
                VStack(alignment: .leading, spacing: 24) {
                    ForEach(sections) { section in
                        VStack(alignment: .leading, spacing: 8) {
                            section.header
                                .font(.system(size: 14, weight: .semibold))
                                .accessibilityAddTraits(.isHeader)
                            SettingsRows { section.content }
                                .background(alignment: .top) {
                                    Rectangle()
                                        .fill(Color.primary.opacity(0.09))
                                        .frame(height: 1)
                                        .allowsHitTesting(false)
                                }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.top, 16)
                .padding(.bottom, 24)
                .padding(.horizontal, gutter)
            }
        }
        .disabled(editor.loading || editor.saving || busy)
    }

    private var settingsHeader: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Text("設定")
                    .font(.system(size: compact ? 23 : 27, weight: .medium))
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 4)
                if !editor.changes.isEmpty || editor.saving {
                    Button(editor.saving ? "保存中" : "変更を保存") {
                        Task { await editor.save() }
                    }
                    .keyboardShortcut("s")
                    .buttonStyle(SettingsActionStyle(tint: accent))
                    .disabled(editor.loading || editor.saving || busy)
                }
            }
            .frame(minHeight: 44)
            tabs
            if let feedback = editor.feedback {
                Text(feedback)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .accessibilityLabel("設定の状態 \(feedback)")
            }
        }
        .padding(.horizontal, gutter)
        .padding(.top, compact ? 16 : 20)
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private var fields: some View {
        switch page {
        case .input: inputSettings
        case .transcription: transcriptionSettings
        case .summary: summarySettings
        case .speakers: speakerSettings
        case .appearance: appearance()
        case .advanced: advancedSettings
        }
    }

    private var tabs: some View {
        let pages = NativeSettingsPage.allCases
        return ViewThatFits(in: .horizontal) {
            tabRow(pages)
            VStack(spacing: 4) {
                tabRow(Array(pages.prefix(3)))
                tabRow(Array(pages.suffix(3)))
            }
        }
        .frame(maxWidth: .infinity)
        .background(alignment: .bottom) {
            Rectangle()
                .fill(Color.primary.opacity(0.12))
                .frame(height: 1)
                .allowsHitTesting(false)
        }
    }

    private func tabRow(_ pages: [NativeSettingsPage]) -> some View {
        HStack(spacing: 4) {
            ForEach(pages) { item in
                let active = page == item
                Button {
                    page = item
                } label: {
                    Text(item.rawValue)
                        .font(.system(size: 13, weight: active ? .semibold : .medium))
                        .foregroundStyle(active ? accent : .secondary)
                        .fixedSize(horizontal: true, vertical: false)
                        .padding(.horizontal, 8)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .contentShape(Rectangle())
                        .overlay(alignment: .bottom) {
                            if active {
                                Capsule()
                                    .fill(accent)
                                    .frame(maxWidth: 72)
                                    .frame(height: 2)
                                    .padding(.horizontal, 8)
                                    .allowsHitTesting(false)
                            }
                        }
                }
                .buttonStyle(ReaderHoverStyle(tint: .primary, motion: motion))
                .accessibilityAddTraits(active ? .isSelected : [])
            }
        }
    }

    @ViewBuilder
    private var inputSettings: some View {
        Section("入力") {
            SettingsRow("マイク") {
                Picker("マイク", selection: Binding(
                    get: { Int(editor.document["recording.mic_device"]?.number ?? -1) },
                    set: { editor.set("recording.mic_device", $0 == -1 ? .null : .number(Double($0))) }
                )) {
                    Text("システムの既定").tag(-1)
                    ForEach(devices) { Text($0.name).tag($0.id) }
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .controlSize(.large)
                .frame(maxWidth: 245, alignment: .trailing)
            }
            flag("システム音声も録音", "recording.last_capture_system", fallback: true)
        }
        Section("停止忘れ通知") {
            flag("無音が続いたら通知", "recording.stop_forget_reminder.enabled", fallback: true)
            numeric("無音の長さ（秒）", "recording.stop_forget_reminder.silence_sec", fallback: 300)
            numeric("無音判定のしきい値", "recording.stop_forget_reminder.level_threshold", fallback: 0.02)
        }
    }

    @ViewBuilder
    private var transcriptionSettings: some View {
        Section("Whisperモデル") {
            WhisperModelRows(downloads: downloads, selection: text("whisper.model", fallback: "medium"),
                             compact: compact, tint: accent) { model in
                confirmation = .init(target: .model, id: model.name, label: model.label)
            }
            if let failure = downloads.failure {
                SettingsRow { Text(failure).font(.callout).foregroundStyle(.secondary) }
            }
        }
        Section("認識") {
            choose("言語", "whisper.language", fallback: "ja",
                   options: [("ja", "日本語"), ("auto", "自動"), ("en", "英語"), ("zh", "中国語"), ("ko", "韓国語")])
            choose("発話の検出", "whisper.streaming.vad_provider", fallback: "silero",
                   options: [("silero", "Silero"), ("energy", "音量")])
        }
        Section("パフォーマンス") {
            choose("動作モード", "whisper.performance.mode", fallback: "auto",
                   options: [("auto", "自動"), ("full", "速度優先"), ("eco", "省電力")])
            if text("whisper.performance.mode", fallback: "auto").wrappedValue == "auto" {
                numeric("CPU使用率（%）", "whisper.performance.cpu_high_threshold", fallback: 75)
                numeric("メモリ使用率（%）", "whisper.performance.memory_high_threshold", fallback: 85)
            }
            SettingsDisclosure(title: "詳細設定") {
                numeric("抑制の強さ", "whisper.performance.throttle_ratio", fallback: 0.5)
                numeric("休止の上限（秒）", "whisper.performance.max_throttle_sec", fallback: 3)
                numeric("処理の優先度", "whisper.performance.worker_nice", fallback: 3)
                numeric("Metalメモリの比率", "whisper.performance.mlx_memory_ratio", fallback: 0.4)
            }
        }
    }

    @ViewBuilder
    private var summarySettings: some View {
        Section("モデル") {
            choose("要約プロバイダ", "minutes_ai.provider", fallback: "ollama",
                   options: SummaryProvider.allCases.map { ($0.rawValue, $0.label) })
            let prefix = "minutes_ai." + provider.rawValue
            SettingsRow("要約モデル") {
                textInput("要約モデル", text(prefix + ".model", fallback: provider.modelHint))
                if !cliOptions.isEmpty {
                    Menu {
                        ForEach(cliOptions) { option in
                            Button(option.displayName) { editor.set(prefix + ".model", .text(option.slug)) }
                        }
                        if provider == .codex { Button("CLIの既定") { editor.set(prefix + ".model", .text("")) } }
                    } label: {
                        Image(systemName: "list.bullet")
                            .frame(width: 34, height: 34)
                    }
                    .menuStyle(.borderlessButton)
                    .menuIndicator(.hidden)
                    .fixedSize()
                    .accessibilityLabel("利用可能なモデル")
                    .help("利用可能なモデル")
                }
            }
            if provider.isCLI {
                SettingsRow("起動コマンド") {
                    textInput("起動コマンド", text(prefix + ".launcher_command"))
                }
                if provider == .codex {
                    choose("推論レベル", prefix + ".reasoning_effort", fallback: "low",
                           options: ["minimal", "low", "medium", "high", "xhigh"].map { ($0, $0) })
                }
            }
            if provider == .ollama {
                numeric("コンテキスト長", prefix + ".num_ctx", fallback: 8192)
            }
            if provider.requiresKey, editor.api != nil {
                SettingsRow("APIキー") {
                    SecureField(hasProviderKey ? "保存済みのAPIキーを更新" : "APIキー", text: $key)
                        .textFieldStyle(.roundedBorder)
                        .controlSize(.large)
                        .frame(maxWidth: 245)
                }
                SettingsRow {
                    actionsLayout {
                        Button("キーを保存") {
                            let value = key.trimmingCharacters(in: .whitespacesAndNewlines)
                            let selectedProvider = provider
                            act { api in
                                try await api.setSummaryKey(value, provider: selectedProvider.rawValue)
                                hasProviderKey = true
                                key = ""
                            }
                        }
                        .disabled(key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        if hasProviderKey {
                            Button("キーを削除", role: .destructive) {
                                let selectedProvider = provider
                                act { api in
                                    try await api.removeSummaryKey(provider: selectedProvider.rawValue)
                                    hasProviderKey = false
                                }
                            }
                        }
                        Button("クラウド要約を許可") { confirmingCloud = true }
                    }
                    .buttonStyle(SettingsActionStyle())
                }
            }
            if editor.api != nil {
                SettingsRow {
                    Button(checkingProvider ? "テスト中" : "接続をテスト", action: testConnection)
                        .buttonStyle(SettingsActionStyle())
                        .disabled(checkingProvider || editor.loading)
                    if checkingProvider {
                        ProgressView().controlSize(.small)
                    }
                    if editor.changes[prefix + ".launcher_command"] != nil {
                        Text("起動コマンドの変更は保存後に反映されます")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                if let message = connectionResult?.message ?? connectionError {
                    SettingsRow {
                        Label(message, systemImage: connectionResult?.ok == true ? "checkmark.circle" : "exclamationmark.circle")
                            .font(.callout)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                    }
                }
            }
        }
        Section("自動要約") {
            flag("録音停止時に要約", "minutes_ai.auto_generate", fallback: true)
            flag("議事録のタイトルも作成", "minutes_ai.generate_title", fallback: true)
            flag("プロジェクトの辞書を更新", "minutes_ai.auto_dictionary_update", fallback: true)
            numeric("タイムアウト（秒）", "minutes_ai.timeout_sec", fallback: 300)
            SettingsDisclosure(title: "要約プロンプト") {
                SettingsRow {
                    TextEditor(text: text("minutes_ai.custom_system_prompt"))
                        .overlayEditorScrollIndicators()
                        .font(.system(size: 13))
                        .scrollContentBackground(.hidden)
                        .frame(minHeight: 112)
                        .accessibilityLabel("要約プロンプト")
                }
                SettingsRow {
                    Button("既定のプロンプトに戻す") { editor.set("minutes_ai.custom_system_prompt", .text("")) }
                        .buttonStyle(SettingsActionStyle())
                }
            }
        }
    }

    @ViewBuilder
    private var speakerSettings: some View {
        let prefix = "whisper.speaker_memory."
        Section("話者分離") {
            flag("話者分離を有効にする", prefix + "diarization_enabled", fallback: true)
            choose("話者分離エンジン", prefix + "diarization_provider", fallback: "legacy",
                   options: [("legacy", "標準"), ("pyannote", "pyannote")])
            if text(prefix + "diarization_provider", fallback: "legacy").wrappedValue == "pyannote" {
                optionalNumber("最少話者数", prefix + "pyannote_min_speakers")
                optionalNumber("最大話者数", prefix + "pyannote_max_speakers")
                choose("デバイス", prefix + "pyannote_device", fallback: "auto",
                       options: [("auto", "自動"), ("cpu", "CPU"), ("mps", "Apple GPU")])
                if editor.api != nil {
                    SettingsRow("Hugging Faceトークン") {
                        SecureField(hasDiarizationToken ? "保存済みのトークンを更新" : "Hugging Faceトークン", text: $diarizationToken)
                            .textFieldStyle(.roundedBorder)
                            .controlSize(.large)
                            .frame(maxWidth: 245)
                    }
                    SettingsRow {
                        actionsLayout {
                            Button("トークンを保存") {
                                let value = diarizationToken
                                act { api in
                                    try await api.setDiarizationToken(value)
                                    hasDiarizationToken = true
                                    diarizationToken = ""
                                }
                            }
                            .disabled(diarizationToken.isEmpty)
                            if hasDiarizationToken {
                                Button("トークンを削除", role: .destructive) {
                                    act { api in try await api.removeDiarizationToken(); hasDiarizationToken = false }
                                }
                            }
                            Button("接続をテスト") {
                                act { api in editor.feedback = try await api.checkDiarization().message }
                            }
                        }
                        .buttonStyle(SettingsActionStyle())
                    }
                }
            }
            flag("話者の名前を記憶", prefix + "enabled", fallback: true)
            numeric("照合のしきい値", prefix + "match_threshold", fallback: 0.82)
            numeric("検出する最小秒数", prefix + "min_audio_sec", fallback: 1)
        }
        if let api = editor.api {
            Section("話者一覧") {
                if profiles.isEmpty {
                    SettingsRow { Text("登録済みの話者はありません").foregroundStyle(.secondary) }
                }
                ForEach(profiles) { profile in
                    SettingsRow {
                        actionsLayout {
                            HStack(spacing: 8) {
                                Toggle("統合する話者を選択", isOn: Binding(
                                    get: { profilesToMerge.contains(profile.id) },
                                    set: { selected in
                                        if selected { profilesToMerge.insert(profile.id) }
                                        else { profilesToMerge.remove(profile.id) }
                                    }
                                ))
                                .toggleStyle(.checkbox)
                                .labelsHidden()

                                textInput("話者名", Binding(get: { profileNames[profile.id] ?? profile.label },
                                                           set: { profileNames[profile.id] = $0 }))
                            }
                            HStack(spacing: 8) {
                                if profileNames[profile.id] != profile.label {
                                    Button("変更") {
                                        let label = profileNames[profile.id] ?? profile.label
                                        act { _ in
                                            try await api.renameSpeaker(profile.id, label: label)
                                            try await loadProfiles(api)
                                        }
                                    }
                                }
                                if profile.sampleAvailable {
                                    Button(speakerPlaying == profile.id && speakerAudio.isPlaying ? "停止" : "サンプル再生") {
                                        if let url = try? api.speakerAudio(for: profile.id) {
                                            speakerPlaying = profile.id
                                            speakerAudio.load(id: profile.id, url: url)
                                            speakerAudio.toggle()
                                        }
                                    }
                                }
                                Button("削除", role: .destructive) {
                                    confirmation = .init(target: .speaker, id: profile.id, label: profile.label)
                                }
                            }
                            .buttonStyle(SettingsActionStyle())
                        }
                    }
                }
                if profilesToMerge.count > 1 {
                    SettingsRow {
                        Menu("選択した話者を統合") {
                            ForEach(profiles.filter { profilesToMerge.contains($0.id) }) { first in
                                Button(first.label + "に統合") {
                                    let sources = Array(profilesToMerge.subtracting([first.id]))
                                    act { _ in
                                        try await api.mergeSpeakers(into: first.id, sourceIds: sources)
                                        profilesToMerge = []
                                        try await loadProfiles(api)
                                    }
                                }
                            }
                        }
                        .controlSize(.large)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var advancedSettings: some View {
        Section("デバッグ") {
            flag("デバッグモード", "debug.enabled", fallback: false)
            choose("ログレベル", "logging.level", fallback: "INFO",
                   options: ["DEBUG", "INFO", "WARNING", "ERROR"].map { ($0, $0) })
        }
    }

    private func choose(_ label: String, _ key: String, fallback: String,
                        options: [(value: String, label: String)]) -> some View {
        let selection = text(key, fallback: fallback)
        return SettingsRow(label) {
            Picker(label, selection: selection) {
                if !options.contains(where: { $0.value == selection.wrappedValue }) {
                    Text(selection.wrappedValue).tag(selection.wrappedValue)
                }
                ForEach(options, id: \.value) { option in Text(option.label).tag(option.value) }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .controlSize(.large)
            .frame(maxWidth: 245, alignment: .trailing)
        }
    }

    private var actionsLayout: AnyLayout {
        compact ? AnyLayout(VStackLayout(alignment: .leading, spacing: 4)) : AnyLayout(HStackLayout(spacing: 8))
    }

    private func text(_ key: String, fallback: String = "") -> Binding<String> {
        Binding(get: { editor.document[key]?.text ?? fallback }, set: { editor.set(key, .text($0)) })
    }

    private func numeric(_ label: String, _ key: String, fallback: Double) -> some View {
        SettingsRow(label) {
            TextField(label, value: Binding(get: { editor.document[key]?.number ?? fallback },
                                           set: { editor.set(key, .number($0)) }), format: .number)
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                .controlSize(.large)
                .multilineTextAlignment(.trailing)
                .monospacedDigit()
                .frame(width: 78)
        }
    }

    private func optionalNumber(_ label: String, _ key: String) -> some View {
        SettingsRow(label) {
            textInput(label, Binding(
                get: { editor.document[key]?.number.map { String(Int($0)) } ?? "" },
                set: { editor.set(key, Double($0).map(SettingValue.number) ?? .null) }
            ), width: 78)
            .multilineTextAlignment(.trailing)
            .monospacedDigit()
        }
    }

    private func textInput(_ label: String, _ value: Binding<String>, width: CGFloat = 245) -> some View {
        TextField(label, text: value)
            .labelsHidden()
            .textFieldStyle(.roundedBorder)
            .controlSize(.large)
            .frame(maxWidth: width)
    }

    private func flag(_ label: String, _ key: String, fallback: Bool) -> some View {
        SettingsToggleRow(label, isOn: Binding(get: { editor.document[key]?.flag ?? fallback },
                                              set: { editor.set(key, .flag($0)) }))
            .tint(accent)
    }

    private func act(_ operation: @escaping @MainActor (SeamAPI) async throws -> Void) {
        guard let api = editor.api, !busy else { return }
        busy = true
        Task {
            defer { busy = false }
            do { try await operation(api) }
            catch { editor.feedback = error.localizedDescription }
        }
    }

    private func refreshPage() async {
        guard let api = editor.api else { return }
        do {
            switch page {
            case .input: devices = try await api.devices().devices
            case .transcription: await downloads.refresh()
            case .speakers:
                hasDiarizationToken = try await api.diarizationStatus().hasHfToken
                try await loadProfiles(api)
            default:
                speakerAudio.clear()
            }
        } catch is CancellationError {} catch {
            editor.feedback = error.localizedDescription
        }
    }

    private func refreshProvider() async {
        guard let api = editor.api else { return }
        key = ""
        cliOptions = []
        hasProviderKey = false
        connectionResult = nil
        connectionError = nil
        let current = provider
        do {
            if current.isCLI {
                let options = try await api.cliModels(for: current.rawValue)
                if !Task.isCancelled && provider == current { cliOptions = options }
            } else if current.requiresKey {
                let keys = try await api.configuredKeys()
                if !Task.isCancelled && provider == current { hasProviderKey = keys[current.rawValue] ?? false }
            }
        } catch is CancellationError {} catch {
            editor.feedback = error.localizedDescription
        }
    }

    private func testConnection() {
        guard let api = editor.api, !checkingProvider else { return }
        let target = connectionTarget
        checkingProvider = true
        connectionResult = nil
        connectionError = nil
        Task {
            defer { checkingProvider = false }
            do {
                let result = try await api.checkSummaryProvider(target.provider.rawValue, model: target.model,
                                                                reasoningEffort: target.effort)
                if connectionTarget == target { connectionResult = result }
            } catch {
                if connectionTarget == target { connectionError = error.localizedDescription }
            }
        }
    }

    @MainActor private func loadProfiles(_ api: SeamAPI) async throws {
        profiles = try await api.speakers()
        profileNames = Dictionary(uniqueKeysWithValues: profiles.map { ($0.id, $0.label) })
    }

    private func remove(_ entry: Removal) {
        switch entry.target {
        case .model:
            Task {
                do { try await downloads.remove(entry.id) }
                catch { editor.feedback = error.localizedDescription }
            }
        case .speaker:
            act { api in
                try await api.removeSpeaker(entry.id)
                try await loadProfiles(api)
            }
        }
    }
}
