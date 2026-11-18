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

private enum SettingsPage: String, CaseIterable, Identifiable {
    case input = "録音", transcription = "文字起こし", summary = "要約", speakers = "話者", appearance = "外観", advanced = "詳細"
    var id: Self { self }
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
public struct NativeSettings<Appearance: View>: View {
    private typealias Stored<Value> = SwiftUI.State<Value>

    @StateObject private var editor: SettingsEditor
    @Stored private var page: SettingsPage = .transcription
    @Stored private var devices: [AudioDevice] = []
    @Stored private var catalog: WhisperModels?
    @Stored private var cliOptions: [CLIModel] = []
    @Stored private var hasProviderKey = false
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

    private let baseWhisperModels = [
        ("tiny", "Tiny"), ("base", "Base"), ("small", "Small"), ("medium", "Medium"),
        ("large-v1", "Large v1"), ("large-v2", "Large v2"), ("large-v3", "Large v3"),
        ("large-v3-turbo", "Large v3 Turbo"),
    ]

    public init(api: SeamAPI?, compact: Bool = false, @ViewBuilder appearance: @escaping () -> Appearance) {
        _editor = StateObject(wrappedValue: SettingsEditor(api: api))
        self.compact = compact
        self.appearance = appearance
    }

    private var provider: SummaryProvider {
        SummaryProvider(rawValue: editor.document["minutes_ai.provider"]?.text ?? "") ?? .ollama
    }

    public var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("設定").font(.system(size: 26, weight: .semibold)).accessibilityAddTraits(.isHeader)
                Spacer()
                Button(editor.saving ? "保存中" : "保存") {
                    Task { await editor.save() }
                }
                .keyboardShortcut("s")
                .buttonStyle(.borderedProminent)
                .disabled(editor.changes.isEmpty || editor.loading || editor.saving || busy)
            }
            .padding(.horizontal, compact ? 15 : 32)
            .padding(.top, 23)
            .padding(.bottom, 13)

            Group {
                if compact { categoryPicker }
                else { categoryPicker.pickerStyle(.segmented) }
            }
            .padding(.horizontal, compact ? 15 : 32)
            .padding(.bottom, 8)

            Form {
                switch page {
                case .input: inputSettings
                case .transcription: transcriptionSettings
                case .summary: summarySettings
                case .speakers: speakerSettings
                case .appearance: appearance()
                case .advanced: advancedSettings
                }
                if let feedback = editor.feedback {
                    Text(feedback)
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("設定の状態 \(feedback)")
                }
            }
            .formStyle(.grouped)
            .disabled(editor.loading || editor.saving || busy)
        }
        .task { await editor.load() }
        .task(id: page) { await refreshPage() }
        .task(id: provider) { await refreshProvider() }
        .task(id: catalog?.download.state) { await watchModelDownload() }
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

    private var categoryPicker: some View {
        Picker("設定カテゴリ", selection: $page) {
            ForEach(SettingsPage.allCases) { Text($0.rawValue).tag($0) }
        }
    }

    @ViewBuilder
    private var inputSettings: some View {
        Section("入力") {
            Picker("マイク", selection: Binding(
                get: { Int(editor.document["recording.mic_device"]?.number ?? -1) },
                set: { editor.set("recording.mic_device", $0 == -1 ? .null : .number(Double($0))) }
            )) {
                Text("システムの既定").tag(-1)
                ForEach(devices) { Text($0.name).tag($0.id) }
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
        Section("認識") {
            choose("Whisperモデル", "whisper.model", fallback: "medium",
                   options: catalog?.models.map { ($0.name, $0.label) } ?? baseWhisperModels)
            choose("言語", "whisper.language", fallback: "ja",
                   options: [("ja", "日本語"), ("auto", "自動"), ("en", "英語"), ("zh", "中国語"), ("ko", "韓国語")])
            choose("発話の検出", "whisper.streaming.vad_provider", fallback: "silero",
                   options: [("silero", "Silero"), ("energy", "音量")])
        }
        if let api = editor.api, let catalog {
            Section("ダウンロード済みモデル") {
                ForEach(catalog.models) { model in
                    HStack {
                        Text(model.label)
                        Spacer()
                        if model.state == "downloading" {
                            if let percent = catalog.download.percent {
                                ProgressView(value: min(100, max(0, percent)), total: 100).frame(width: 90)
                            } else { ProgressView().controlSize(.small) }
                        } else if model.downloaded {
                            Button("削除") {
                                confirmation = .init(target: .model, id: model.name, label: model.label)
                            }
                            .disabled(model.state == "loaded")
                        } else {
                            Button("ダウンロード") {
                                act { _ in
                                    try await api.downloadWhisper(model.name)
                                    self.catalog = try await api.whisperModels()
                                }
                            }
                        }
                    }
                }
                if let error = catalog.download.error { Text(error).foregroundStyle(.secondary) }
            }
        }
        Section("パフォーマンス") {
            choose("動作モード", "whisper.performance.mode", fallback: "auto",
                   options: [("auto", "自動"), ("full", "速度優先"), ("eco", "省電力")])
            if text("whisper.performance.mode", fallback: "auto").wrappedValue == "auto" {
                numeric("CPU使用率（%）", "whisper.performance.cpu_high_threshold", fallback: 75)
                numeric("メモリ使用率（%）", "whisper.performance.memory_high_threshold", fallback: 85)
            }
            DisclosureGroup("詳細") {
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
            TextField("要約モデル", text: text(prefix + ".model", fallback: provider.modelHint))
            if !cliOptions.isEmpty {
                Menu("利用可能なモデル") {
                    ForEach(cliOptions) { option in
                        Button(option.displayName) { editor.set(prefix + ".model", .text(option.slug)) }
                    }
                    if provider == .codex { Button("CLIの既定") { editor.set(prefix + ".model", .text("")) } }
                }
            }
            if provider.isCLI {
                TextField("起動コマンド", text: text(prefix + ".launcher_command"))
                if provider == .codex {
                    choose("推論レベル", prefix + ".reasoning_effort", fallback: "low",
                           options: ["minimal", "low", "medium", "high", "xhigh"].map { ($0, $0) })
                }
            }
            if provider == .ollama {
                numeric("コンテキスト長", prefix + ".num_ctx", fallback: 8192)
            }
            if provider.requiresKey, editor.api != nil {
                SecureField(hasProviderKey ? "保存済みのAPIキーを更新" : "APIキー", text: $key)
                HStack {
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
            }
            if editor.api != nil {
                Button("接続をテスト") {
                    let selectedProvider = provider
                    act { api in editor.feedback = try await api.checkSummaryProvider(selectedProvider.rawValue).message }
                }
            }
        }
        Section("自動要約") {
            flag("録音停止時に要約", "minutes_ai.auto_generate", fallback: true)
            flag("議事録のタイトルも作成", "minutes_ai.generate_title", fallback: true)
            flag("プロジェクトの辞書を更新", "minutes_ai.auto_dictionary_update", fallback: true)
            numeric("タイムアウト（秒）", "minutes_ai.timeout_sec", fallback: 300)
            DisclosureGroup("要約プロンプト") {
                TextEditor(text: text("minutes_ai.custom_system_prompt"))
                    .frame(minHeight: 112)
                    .accessibilityLabel("要約プロンプト")
                Button("既定のプロンプトに戻す") { editor.set("minutes_ai.custom_system_prompt", .text("")) }
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
                    SecureField(hasDiarizationToken ? "保存済みのトークンを更新" : "Hugging Faceトークン", text: $diarizationToken)
                    HStack {
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
                }
            }
            flag("話者の名前を記憶", prefix + "enabled", fallback: true)
            numeric("照合のしきい値", prefix + "match_threshold", fallback: 0.82)
            numeric("検出する最小秒数", prefix + "min_audio_sec", fallback: 1)
        }
        if let api = editor.api {
            Section("話者一覧") {
                if profiles.isEmpty { Text("登録済みの話者はありません").foregroundStyle(.secondary) }
                ForEach(profiles) { profile in
                    HStack {
                        Toggle("統合する話者を選択", isOn: Binding(
                            get: { profilesToMerge.contains(profile.id) },
                            set: { selected in
                                if selected { profilesToMerge.insert(profile.id) }
                                else { profilesToMerge.remove(profile.id) }
                            }
                        )).toggleStyle(.checkbox).labelsHidden()
                        TextField("話者名", text: Binding(get: { profileNames[profile.id] ?? profile.label },
                                                       set: { profileNames[profile.id] = $0 }))
                        if profileNames[profile.id] != profile.label {
                            Button("変更") {
                                let label = profileNames[profile.id] ?? profile.label
                                act { _ in try await api.renameSpeaker(profile.id, label: label); try await loadProfiles(api) }
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
                        Button("削除") { confirmation = .init(target: .speaker, id: profile.id, label: profile.label) }
                    }
                }
                if profilesToMerge.count > 1 {
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
                }
            }
        }
    }

    @ViewBuilder
    private var advancedSettings: some View {
        Section("アプリ") {
            flag("起動時に更新を確認", "app_update.check_on_startup", fallback: true)
            flag("起動時に自動アップデート", "app_update.auto_install_on_startup", fallback: false)
        }
        Section("デバッグ") {
            flag("デバッグモード", "debug.enabled", fallback: false)
            choose("ログレベル", "logging.level", fallback: "INFO",
                   options: ["DEBUG", "INFO", "WARNING", "ERROR"].map { ($0, $0) })
        }
    }

    private func choose(_ label: String, _ key: String, fallback: String,
                        options: [(value: String, label: String)]) -> some View {
        let selection = text(key, fallback: fallback)
        return Picker(label, selection: selection) {
            if !options.contains(where: { $0.value == selection.wrappedValue }) {
                Text(selection.wrappedValue).tag(selection.wrappedValue)
            }
            ForEach(options, id: \.value) { option in Text(option.label).tag(option.value) }
        }
    }

    private func text(_ key: String, fallback: String = "") -> Binding<String> {
        Binding(get: { editor.document[key]?.text ?? fallback }, set: { editor.set(key, .text($0)) })
    }

    private func numeric(_ label: String, _ key: String, fallback: Double) -> some View {
        TextField(label, value: Binding(get: { editor.document[key]?.number ?? fallback },
                                       set: { editor.set(key, .number($0)) }), format: .number)
    }

    private func optionalNumber(_ label: String, _ key: String) -> some View {
        TextField(label, text: Binding(
            get: { editor.document[key]?.number.map { String(Int($0)) } ?? "" },
            set: { editor.set(key, Double($0).map(SettingValue.number) ?? .null) }
        ))
    }

    private func flag(_ label: String, _ key: String, fallback: Bool) -> some View {
        Toggle(label, isOn: Binding(get: { editor.document[key]?.flag ?? fallback },
                                   set: { editor.set(key, .flag($0)) }))
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
            case .transcription:
                catalog = try await api.whisperModels()
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

    private func watchModelDownload() async {
        guard let api = editor.api else { return }
        do {
            while catalog?.download.state == "downloading" && !Task.isCancelled {
                try await Task.sleep(for: .seconds(1))
                catalog = try await api.whisperModels()
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

    @MainActor private func loadProfiles(_ api: SeamAPI) async throws {
        profiles = try await api.speakers()
        profileNames = Dictionary(uniqueKeysWithValues: profiles.map { ($0.id, $0.label) })
    }

    private func remove(_ entry: Removal) {
        act { api in
            switch entry.target {
            case .model:
                try await api.removeWhisper(entry.id)
                catalog = try await api.whisperModels()
            case .speaker:
                try await api.removeSpeaker(entry.id)
                try await loadProfiles(api)
            }
        }
    }
}
