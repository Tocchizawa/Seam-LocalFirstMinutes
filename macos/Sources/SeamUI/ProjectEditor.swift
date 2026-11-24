import AppKit
import SwiftUI
import UniformTypeIdentifiers

#if SWIFT_PACKAGE
import SeamCore
#endif

/// サイドバーに置く「新規プロジェクト」ボタン。
public struct NewProjectButton: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let action: () -> Void

    public init(action: @escaping () -> Void) { self.action = action }

    public var body: some View {
        Button(action: action) {
            Label("新規プロジェクト", systemImage: "plus")
                .font(.system(size: 13, weight: .medium))
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 9)
                .frame(height: 35)
                .contentShape(Rectangle())
                .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
        .keyboardShortcut("n", modifiers: [.command, .shift])
        .help("新規プロジェクト ⇧⌘N")
    }
}

/// プロジェクト名、色、保存先、参照資料を編集するフォーム。
@available(macOS 15.0, *)
public struct ProjectEditor: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @FocusState private var nameFocused: Bool
    @FocusState private var emojiFocused: Bool
    @Stored private var name = ""
    @Stored private var output = ""
    @Stored private var emoji = ""
    @Stored private var emojiInput = ""
    @Stored private var choosingEmoji = false
    @Stored private var accent: ProjectAccent?
    @Stored private var repo = ""
    @Stored private var terms = ""
    @Stored private var documents: [String] = []
    @Stored private var choosing = false
    @Stored private var folder: Folder = .output
    @Stored private var saving = false
    @Stored private var problem: String?
    @Stored private var corrections: [Correction] = []
    @Stored private var suggestions: [String] = []
    @Stored private var suggesting = false
    @Stored private var referencesExpanded = false

    private enum Folder { case output, repo, documents }
    private struct Correction: Identifiable {
        let id = UUID()
        var wrong: String
        var correct: String
    }

    private let project: Project?
    private let compact: Bool
    private let suggest: (@MainActor () async throws -> [String])?
    private let cancel: () -> Void
    private let save: @MainActor (ProjectDraft) async throws -> Void

    public init(project: Project? = nil, compact: Bool = false, suggest: (@MainActor () async throws -> [String])? = nil,
                cancel: @escaping () -> Void, save: @escaping @MainActor (ProjectDraft) async throws -> Void) {
        self.project = project
        self.compact = compact
        self.suggest = suggest
        self.cancel = cancel
        self.save = save
        _name = Stored(initialValue: project?.name ?? "")
        _output = Stored(initialValue: project?.outputDir ?? "")
        _emoji = Stored(initialValue: project?.emoji ?? "")
        _accent = Stored(initialValue: project?.color.flatMap(ProjectAccent.init(rawValue:)))
        _repo = Stored(initialValue: project?.repoPath ?? "")
        _terms = Stored(initialValue: project?.glossary?.joined(separator: "\n") ?? "")
        _documents = Stored(initialValue: project?.docDirs ?? [])
        _corrections = Stored(initialValue: (project?.corrections ?? []).map { Correction(wrong: $0.wrong, correct: $0.correct) })
    }

    private var gutter: CGFloat { compact ? 16 : 28 }
    private var motion: Animation? { reduceMotion ? nil : SeamMotion.reposition }

    public var body: some View {
        editorForm
            .scrollEdgeBar { heading }
            .scrollEdgeBar(.bottom) { footer }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.seamPaper(for: colorScheme))
        .interactiveDismissDisabled(saving)
        .task { nameFocused = true }
        .fileImporter(isPresented: $choosing, allowedContentTypes: [.folder], allowsMultipleSelection: folder == .documents) {
            handleFolderSelection($0)
        }
    }

    private var heading: some View {
        Text(project == nil ? "新規プロジェクト" : "プロジェクトを編集")
            .font(.system(size: compact ? 22 : 26, weight: .semibold))
            .accessibilityAddTraits(.isHeader)
            .padding(.horizontal, gutter)
            .padding(.top, 20)
            .padding(.bottom, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var editorForm: some View {
        SeamScrollView { editorFields }
            .scrollBounceBehavior(.basedOnSize)
            .frame(maxHeight: .infinity)
            .textFieldStyle(.plain)
            .disabled(saving)
    }

    // フォーム項目の並び。項目の追加や順番の変更はここを編集する。
    private var editorFields: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 16) {
                nameField
                colorField
                outputField
            }
            .padding(16)
            .seamFieldSurface(radius: 18)
            referencesSection
            if let problem {
                Label(problem, systemImage: "exclamationmark.circle")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
        }
        .frame(maxWidth: 590, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, gutter)
        .padding(.top, 4)
        .padding(.bottom, 24)
    }

    private var outputField: some View {
        field("書き出し先") {
            pathField("書き出し先", placeholder: "~/Documents/議事録", value: $output, target: .output)
        }
    }

    private var referencesSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(motion) { referencesExpanded.toggle() }
            } label: {
                HStack(spacing: 10) {
                    Label(project == nil ? "参照資料" : "参照資料と用語集", systemImage: "folder")
                        .font(.system(size: 13, weight: .semibold))
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .rotationEffect(.degrees(referencesExpanded ? 90 : 0))
                        .foregroundStyle(.secondary)
                }
                .padding(14)
                .contentShape(Rectangle())
            }
            .buttonStyle(ReaderHoverStyle(tint: .primary, motion: motion, cornerRadius: 0))
            .accessibilityValue(referencesExpanded ? "展開済み" : "折りたたみ")
            if referencesExpanded {
                referenceFields
                    .padding(.horizontal, 16)
                    .padding(.bottom, 16)
                    .transition(reduceMotion ? .identity : .opacity)
            }
        }
        .clipShape(.rect(cornerRadius: 18))
        .seamFieldSurface(radius: 18)
    }

    private var referenceFields: some View {
        VStack(alignment: .leading, spacing: 16) {
            repositoryField
            documentsField
            if project != nil {
                glossaryField
                correctionsField
            }
        }
    }

    private var repositoryField: some View {
        field("リポジトリ") {
            pathField("リポジトリ", placeholder: "任意", value: $repo, target: .repo)
        }
    }

    private var documentsField: some View {
        field("資料フォルダ") {
            ForEach(documents, id: \.self) { path in
                HStack {
                    Text(verbatim: path).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button { documents.removeAll { $0 == path } } label: {
                        Image(systemName: "minus.circle").frame(width: 24, height: 22)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("資料フォルダを選択から外す \(path)")
                }
            }
            Button("資料フォルダを追加…", systemImage: "plus") { choose(.documents) }
                .buttonStyle(SeamFormActionStyle())
        }
    }

    private var glossaryField: some View {
        field("用語集") {
            TextEditor(text: $terms)
                .overlayEditorScrollIndicators()
                .scrollContentBackground(.hidden)
                .font(.system(size: 13))
                .frame(height: 84)
                .padding(8)
                .seamFieldSurface()
                .accessibilityLabel("用語集、1行に1語")
            if suggest != nil { glossarySuggestionControls }
        }
    }

    @ViewBuilder
    private var glossarySuggestionControls: some View {
        Button(suggesting ? "抽出中" : "保存済みの資料から候補を抽出", action: extractGlossarySuggestions)
            .buttonStyle(SeamFormActionStyle())
            .disabled(suggesting)
        if !suggestions.isEmpty {
            Text(suggestions.joined(separator: "、")).font(.callout)
            Button("候補を追加", systemImage: "plus", action: appendGlossarySuggestions)
                .buttonStyle(SeamFormActionStyle())
        }
    }

    private var correctionsField: some View {
        field("誤転写の補正") {
            ForEach($corrections) { $rule in
                HStack {
                    TextField("誤表記", text: $rule.wrong).padding(8).seamFieldSurface()
                    Image(systemName: "arrow.right").font(.caption).foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                    TextField("正しい表記", text: $rule.correct).padding(8).seamFieldSurface()
                    Button { corrections.removeAll { $0.id == rule.id } } label: {
                        Image(systemName: "minus.circle")
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("補正を削除")
                }
            }
            Button("補正を追加", systemImage: "plus") { corrections.append(Correction(wrong: "", correct: "")) }
                .buttonStyle(SeamFormActionStyle())
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Spacer()
            if saving { ProgressView().controlSize(.small) }
            Button("キャンセル", action: cancel)
                .buttonStyle(SeamFormActionStyle())
                .disabled(saving)
                .keyboardShortcut(.cancelAction)
            Button(project == nil ? "作成" : "保存", systemImage: "checkmark", action: submit)
                .buttonStyle(SeamFormActionStyle(primary: true))
                .disabled(saving || cleaned(name).isEmpty || cleaned(output).isEmpty)
                .keyboardShortcut(.defaultAction)
        }
        .padding(.horizontal, gutter)
        .padding(.vertical, 12)
    }

    private var nameField: some View {
        field("プロジェクト名") {
            HStack(spacing: 8) {
                Button {
                    emojiInput = ""
                    choosingEmoji.toggle()
                } label: {
                    HStack(spacing: 2) {
                        ProjectMark(emoji: emoji, color: accent?.rawValue, size: 28)
                        Image(systemName: "chevron.down")
                            .font(.system(size: 8, weight: .semibold))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 5)
                    .frame(height: 34)
                    .seamFieldSurface(radius: 10)
                    .contentShape(Rectangle())
                }
                .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
                .accessibilityLabel("プロジェクトのアイコンを選ぶ")
                .help("プロジェクトのアイコンを選ぶ")
                .popover(isPresented: $choosingEmoji, arrowEdge: .bottom) { emojiPicker }

                TextField("プロジェクト名", text: $name, prompt: Text("名前を入力"))
                    .labelsHidden()
                    .font(.system(size: 15))
                    .accessibilityLabel("プロジェクト名")
                    .focused($nameFocused)
                    .padding(.horizontal, 10)
                    .frame(height: 36)
                    .seamTextPointer()
                .seamFieldSurface(focused: nameFocused)
            }
        }
    }

    // フォルダの色と絵文字を選ぶ領域。候補の追加は emojiGrid と ProjectAccent を見る。
    private var colorField: some View {
        field("色") {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 30, maximum: 32), spacing: 3)], spacing: 3) {
                colorButton(nil)
                ForEach(ProjectAccent.allCases, id: \.self) { colorButton($0) }
            }
            .frame(maxWidth: 318, alignment: .leading)
        }
    }

    private var emojiPicker: some View {
        return VStack(alignment: .leading, spacing: 12) {
            emojiGrid
            Divider()
            emojiEntry
            if !emoji.isEmpty {
                Button("フォルダに戻す") {
                    emoji = ""
                    choosingEmoji = false
                }
                .buttonStyle(.link)
            }
        }
        .padding(12)
        .frame(width: 245)
    }

    private var emojiGrid: some View {
        let symbols: [String] = ["📝", "💬", "🎙️", "📅", "📁", "📌", "🧭", "💡", "🎯", "🛠️", "💻", "🚀",
                                 "📊", "🤝", "🏢", "📚", "🎨", "⭐"]
        let columns = Array(repeating: GridItem(.fixed(33), spacing: 4), count: 6)
        return LazyVGrid(columns: columns, spacing: 4) {
            ForEach(symbols, id: \.self) { symbol in emojiChoice(symbol) }
        }
    }

    private func emojiChoice(_ symbol: String) -> some View {
        let selected = emoji == symbol
        return Button {
            emoji = symbol
            choosingEmoji = false
        } label: {
            Text(verbatim: symbol)
                .font(.system(size: 19))
                .frame(width: 33, height: 32)
                .background(.primary.opacity(selected ? 0.11 : 0), in: .rect(cornerRadius: 7))
                .contentShape(Rectangle())
        }
        .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var emojiEntry: some View {
        HStack(spacing: 6) {
            emojiTextField
            Button(action: openCharacterPalette) {
                Image(systemName: "face.smiling").frame(width: 25, height: 24)
            }
            .accessibilityLabel("絵文字ビューアを開く")
            .help("Macの絵文字ビューアを開く")
        }
    }

    private var emojiTextField: some View {
        TextField("絵文字を入力", text: $emojiInput)
            .textFieldStyle(.plain)
            .focused($emojiFocused)
            .padding(8)
            .seamTextPointer()
            .seamFieldSurface(focused: emojiFocused)
            .accessibilityLabel("絵文字を入力")
            .onChange(of: emojiInput) { oldValue, newValue in
                updateEmojiInput(oldValue, newValue)
            }
            .onSubmit { choosingEmoji = false }
    }

    private func updateEmojiInput(_ oldValue: String, _ value: String) {
        let symbol = String(cleaned(value).prefix(1))
        if symbol != value { emojiInput = symbol }
        if !symbol.isEmpty { emoji = symbol }
    }

    private func openCharacterPalette() {
        emojiFocused = true
        Task { @MainActor in
            await Task.yield()
            NSApp.orderFrontCharacterPalette(nil)
        }
    }

    private func colorButton(_ choice: ProjectAccent?) -> some View {
        let selected = accent == choice
        let tint = choice?.tint ?? .secondary
        return Button { accent = choice } label: {
            ZStack {
                Circle().strokeBorder(tint.opacity(selected ? 0.92 : 0), lineWidth: 1.5)
                Circle().fill(tint.opacity(choice == nil ? 0.13 : 0.86)).padding(4)
                if choice == nil {
                    Image(systemName: "minus")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: 27, height: 27)
            .frame(width: 32, height: 32)
            .contentShape(Rectangle())
        }
        .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
        .accessibilityLabel(choice?.label ?? "標準色")
        .accessibilityAddTraits(selected ? .isSelected : [])
        .help(choice?.label ?? "標準色")
    }

    private func field<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
            content()
        }
    }

    private func pathField(_ label: String, placeholder: String, value: Binding<String>, target: Folder) -> some View {
        HStack(spacing: 8) {
            TextField(label, text: value, prompt: Text(placeholder))
                .labelsHidden()
                .font(.system(size: 13))
                .accessibilityLabel(label)
                .padding(.horizontal, 10)
                .frame(height: 36)
                .seamTextPointer()
                .seamFieldSurface()
            Button("選択…", systemImage: "folder") { choose(target) }
                .buttonStyle(SeamFormActionStyle())
                .accessibilityLabel("\(label)のフォルダを選択")
                .help("\(label)のフォルダを選択")
        }
    }

    private func choose(_ target: Folder) {
        folder = target
        choosing = true
    }

    private func handleFolderSelection(_ result: Result<[URL], Error>) {
        do {
            let paths = try result.get().map(\.path)
            guard let first = paths.first else { return }
            switch folder {
            case .output: output = first
            case .repo: repo = first
            case .documents: documents += paths.filter { !documents.contains($0) }
            }
        } catch {
            problem = "フォルダを開けませんでした。"
        }
    }

    private func extractGlossarySuggestions() {
        guard let suggest, !suggesting else { return }
        suggesting = true
        problem = nil
        Task {
            defer { suggesting = false }
            do {
                let existing = Set(terms.split(whereSeparator: \.isNewline).map { cleaned(String($0)) })
                suggestions = try await suggest().filter { !existing.contains($0) }
                if suggestions.isEmpty { problem = "新しい用語候補はありませんでした。" }
            } catch {
                problem = error.localizedDescription
            }
        }
    }

    private func appendGlossarySuggestions() {
        terms = ([terms].filter { !$0.isEmpty } + suggestions).joined(separator: "\n")
        suggestions = []
    }

    private func cleaned(_ value: String) -> String { value.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func submit() {
        guard !saving else { return }
        saving = true
        problem = nil
        let repository = cleaned(repo)
        let rules = corrections.map { ProjectCorrection(wrong: cleaned($0.wrong), correct: cleaned($0.correct)) }
        guard rules.allSatisfy({ !$0.wrong.isEmpty && !$0.correct.isEmpty }) else {
            problem = "補正の誤表記と正しい表記を入力してください。"
            saving = false
            return
        }
        let draft = ProjectDraft(name: cleaned(name), outputDir: cleaned(output),
                                 repoPath: repository.isEmpty ? nil : repository,
                                 docDirs: documents,
                                 glossary: terms.split(whereSeparator: \.isNewline).map { cleaned(String($0)) }.filter { !$0.isEmpty },
                                 corrections: project == nil ? nil : rules,
                                 emoji: emoji.isEmpty ? nil : emoji, color: accent?.rawValue)
        Task {
            defer { saving = false }
            do { try await save(draft) }
            catch { problem = error.localizedDescription }
        }
    }
}

#Preview("プロジェクト作成") {
    if #available(macOS 15.0, *) {
        ProjectEditor(cancel: {}, save: { _ in })
            .frame(width: 840, height: 700)
    }
}
