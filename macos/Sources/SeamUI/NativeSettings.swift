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
    case input = "録音", transcription = "文字起こし", summary = "要約", appearance = "外観", advanced = "詳細"
    public var id: Self { self }

    fileprivate var symbol: String {
        switch self {
        case .input: "mic"
        case .transcription: "waveform"
        case .summary: "doc.text"
        case .appearance: "circle.lefthalf.filled"
        case .advanced: "slider.horizontal.3"
        }
    }
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

/// アプリ内の設定画面。保存時は変更した項目だけをバックエンドへ送る。
@available(macOS 15.0, *)
public struct NativeSettings<Appearance: View>: View {
    @Environment(\.appUpdateControls) private var updates
    private typealias Stored<Value> = SwiftUI.State<Value>

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @Namespace private var selectedCategory
    @StateObject private var editor: SettingsEditor
    @Binding private var page: NativeSettingsPage
    @Stored private var devices: [AudioDevice] = []
    @Stored private var cliOptions: [CLIModel] = []
    @Stored private var hasProviderKey = false
    @Stored private var checkingProvider = false
    @Stored private var connectionResult: ServiceConnectionCheck?
    @Stored private var connectionError: String?
    @Stored private var key = ""
    @Stored private var confirmingCloud = false
    @Stored private var busy = false

    private let appearance: () -> Appearance
    private let compact: Bool

    private struct ConnectionTarget: Equatable {
        let provider: SummaryProvider
        let model: String?
        let effort: String?
    }

    public init(api: SeamAPI?, page: Binding<NativeSettingsPage>, compact: Bool = false,
                @ViewBuilder appearance: @escaping () -> Appearance) {
        _editor = StateObject(wrappedValue: SettingsEditor(api: api))
        self._page = page
        self.compact = compact
        self.appearance = appearance
    }

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

    // 設定ページ左右の余白。狭い画面用と通常幅を別々に調整できる。
    private var gutter: CGFloat { compact ? 16 : 32 }

    public var body: some View {
        // 選択中のページを表示する外枠。各設定項目は下のページ別プロパティに分かれている。
        ZStack(alignment: .topLeading) {
            settingsContent
                .id(page)
                .transition(.identity)
        }
        .animatePageChange(for: page, enabled: !reduceMotion)
        .scrollEdgeBar { settingsHeader }
        .scrollEdgeBar(.bottom, blur: false) { settingsFooter }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .tint(.primary)
        .task { await editor.load() }
        .task(id: page) { await refreshPage() }
        .task(id: provider) { await refreshProvider() }
        .onChange(of: connectionTarget) { _, _ in
            connectionResult = nil
            connectionError = nil
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

    // 現在選択中の設定ページの内容をスクロール表示する。
    private var settingsContent: some View {
        SeamScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .center, spacing: 16) {
                    Text(page.rawValue)
                        .font(.system(size: compact ? 24 : 30, weight: .semibold))
                        .accessibilityAddTraits(.isHeader)
                    Spacer(minLength: 0)
                    if !editor.changes.isEmpty || editor.saving {
                        saveButton
                            .transition(reduceMotion ? .identity : .move(edge: .trailing).combined(with: .opacity))
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .animation(reduceMotion ? nil : SeamMotion.reposition,
                           value: !editor.changes.isEmpty || editor.saving)

                if editor.loading {
                    ProgressView("設定を読み込み中")
                        .controlSize(.small)
                        .foregroundStyle(.secondary)
                } else {
                    Group(sections: fields) { sections in
                        ForEach(sections) { section in
                            VStack(alignment: .leading, spacing: 8) {
                                section.header
                                    .font(.system(size: 13, weight: .semibold))
                                    .accessibilityAddTraits(.isHeader)
                                    .padding(.horizontal, 4)
                                SettingsRows { section.content }
                                    .background(Color.primary.opacity(colorScheme == .dark ? 0.045 : 0.028),
                                                in: RoundedRectangle(cornerRadius: 16))
                                    .clipShape(RoundedRectangle(cornerRadius: 16))
                                    .overlay {
                                        RoundedRectangle(cornerRadius: 16)
                                            .strokeBorder(Color.primary.opacity(0.08))
                                            .allowsHitTesting(false)
                                    }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 20)
            .padding(.horizontal, gutter)
        }
        .disabled(editor.loading || editor.saving || busy)
    }

    private var settingsHeader: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) {
                Text("設定")
                    .font(.system(size: 15, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 4)
                categoryTabs
            }
            HStack(spacing: 12) {
                Text("設定")
                    .font(.system(size: 15, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 4)
                settingsGlass {
                    Menu {
                        ForEach(NativeSettingsPage.allCases) { item in
                            Button(item.rawValue, systemImage: page == item ? "checkmark" : item.symbol) {
                                page = item
                            }
                        }
                    } label: {
                        Label(page.rawValue, systemImage: page.symbol)
                            .font(.system(size: 13, weight: .medium))
                            .padding(.horizontal, 10)
                            .frame(height: 36)
                    }
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                    .accessibilityLabel("設定カテゴリ")
                }
            }
        }
        .padding(.horizontal, gutter)
        .padding(.top, gutter)
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private var settingsFooter: some View {
        if let feedback = editor.feedback {
            HStack {
                Text(feedback)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .accessibilityLabel("設定の状態 \(feedback)")
                Spacer(minLength: 0)
            }
            .padding(.horizontal, gutter)
            .padding(.vertical, 8)
            .background(Color.seamPaper(for: colorScheme))
        }
    }

    private var saveButton: some View {
        Button(editor.saving ? "保存中" : "変更を保存", systemImage: "checkmark") {
            Task { await editor.save() }
        }
        .keyboardShortcut("s")
        .buttonStyle(SettingsActionStyle(tint: accent))
        .disabled(editor.loading || editor.saving || busy)
    }

    @ViewBuilder
    private var fields: some View {
        switch page {
        case .input: inputSettings
        case .transcription: transcriptionSettings
        case .summary: summarySettings
        case .appearance: appearance()
        case .advanced: advancedSettings
        }
    }

    private var categoryTabs: some View {
        settingsGlass {
            HStack(spacing: 2) {
                ForEach(NativeSettingsPage.allCases) { item in
                    let active = page == item
                    Button {
                        page = item
                    } label: {
                        Label(item.rawValue, systemImage: item.symbol)
                            .font(.system(size: 12, weight: active ? .semibold : .medium))
                            .foregroundStyle(active ? Color.primary : Color.secondary)
                            .padding(.horizontal, 9)
                            .frame(height: 34)
                            .background {
                                if active {
                                    Capsule()
                                        .fill(Color.primary.opacity(0.11))
                                        .matchedGeometryEffect(id: "category", in: selectedCategory)
                                }
                            }
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(active ? .isSelected : [])
                }
            }
            .padding(3)
            .fixedSize()
            .animation(reduceMotion ? nil : SeamMotion.reposition, value: page)
        }
    }

    @ViewBuilder
    private func settingsGlass<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        if #available(macOS 26.0, *) {
            content().glassEffect(.regular, in: .capsule)
        } else {
            content().background(.regularMaterial, in: Capsule())
        }
    }

    @ViewBuilder
    // 録音入力と無音通知の設定項目。
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
        Section("Apple Speech") {
            SettingsRow {
                Text("macOS のオンデバイス音声認識を使い、録音中から文字起こしを表示します。")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            choose("言語", "speech.language", fallback: "ja",
                   options: [("ja", "日本語"), ("en", "英語"), ("zh-Hans", "中国語"), ("ko", "韓国語"), ("auto", "システム設定")])
        }
    }

    @ViewBuilder
    // 要約プロバイダー、モデル、接続テストの設定項目。
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
                    fieldSurface {
                        SecureField(hasProviderKey ? "保存済みのAPIキーを更新" : "APIキー", text: $key)
                    }
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
                        .padding(8)
                        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
                        .padding(.vertical, 8)
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
    private var advancedSettings: some View {
        Section("Seam") {
            SettingsRow("バージョン") {
                Text(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "—")
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            if let updates {
                SettingsRow("ソフトウェアアップデート") {
                    Button("今すぐ確認", action: updates.check)
                        .buttonStyle(SettingsActionStyle())
                        .disabled(!updates.canCheck)
                }
                SettingsToggleRow("アップデートを自動で確認", isOn: updates.automaticallyChecks)
                SettingsToggleRow("アップデートを自動でインストール", isOn: updates.automaticallyInstalls)
                    .disabled(!updates.automaticallyChecks.wrappedValue)
            } else if let url = URL(string: "https://github.com/Tocchizawa/Seam-LocalFirstMinutes/releases") {
                SettingsRow("ソフトウェアアップデート") {
                    Link("リリースを表示", destination: url)
                        .buttonStyle(SettingsActionStyle())
                }
            }
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
            fieldSurface {
                TextField(label, value: Binding(get: { editor.document[key]?.number ?? fallback },
                                               set: { editor.set(key, .number($0)) }), format: .number)
                    .multilineTextAlignment(.trailing)
                    .monospacedDigit()
            }
            .frame(width: 88)
        }
    }

    private func textInput(_ label: String, _ value: Binding<String>, width: CGFloat = 245) -> some View {
        fieldSurface { TextField(label, text: value) }
            .frame(maxWidth: width)
    }

    private func fieldSurface<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        content()
            .labelsHidden()
            .textFieldStyle(.plain)
            .padding(.horizontal, 10)
            .frame(height: 32)
            .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 8))
            .overlay {
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(Color.primary.opacity(0.08))
                    .allowsHitTesting(false)
            }
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
            if page == .input { devices = try await api.devices().devices }
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

}
