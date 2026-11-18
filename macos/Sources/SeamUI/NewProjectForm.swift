import SwiftUI
import UniformTypeIdentifiers

#if SWIFT_PACKAGE
import SeamCore
#endif

public struct NewProjectButton: View {
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
        .buttonStyle(ReaderHoverStyle(tint: .primary, motion: .easeOut(duration: 0.13)))
        .keyboardShortcut("n", modifiers: [.command, .shift])
        .help("新規プロジェクト ⇧⌘N")
    }
}

@available(macOS 14.0, *)
public struct NewProjectForm: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.dismiss) private var dismiss
    @FocusState private var nameFocused: Bool
    @Stored private var name = ""
    @Stored private var output = ""
    @Stored private var repo = ""
    @Stored private var terms = ""
    @Stored private var documents: [String] = []
    @Stored private var choosing = false
    @Stored private var folder: Folder = .output
    @Stored private var saving = false
    @Stored private var problem: String?

    private enum Folder { case output, repo, documents }
    private let create: @MainActor (ProjectDraft) async throws -> Void

    public init(create: @escaping @MainActor (ProjectDraft) async throws -> Void) {
        self.create = create
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    field("プロジェクト名") {
                        TextField("プロジェクト名", text: $name, prompt: Text("名前を入力"))
                            .labelsHidden()
                            .focused($nameFocused)
                    }
                    field("書き出し先") {
                        pathField("書き出し先", placeholder: "~/Documents/議事録", value: $output, target: .output)
                    }
                    DisclosureGroup("参照資料と用語集") {
                        VStack(alignment: .leading, spacing: 16) {
                            field("リポジトリ") {
                                pathField("リポジトリ", placeholder: "任意", value: $repo, target: .repo)
                            }
                            field("資料フォルダ") {
                                ForEach(documents, id: \.self) { path in
                                    HStack {
                                        Text(verbatim: path).lineLimit(1).truncationMode(.middle)
                                        Spacer()
                                        Button { documents.removeAll { $0 == path } } label: {
                                            Image(systemName: "minus.circle")
                                                .frame(width: 24, height: 22)
                                        }
                                        .buttonStyle(.plain)
                                        .accessibilityLabel("資料フォルダを選択から外す \(path)")
                                    }
                                }
                                Button("資料フォルダを追加…") { choose(.documents) }
                            }
                            field("用語集") {
                                TextEditor(text: $terms)
                                    .frame(height: 75)
                                    .padding(3)
                                    .overlay { RoundedRectangle(cornerRadius: 5).strokeBorder(.quaternary) }
                                    .accessibilityLabel("用語集、1行に1語")
                            }
                        }
                        .padding(.top, 12)
                    }
                    .font(.system(size: 13, weight: .medium))
                    if let problem {
                        Text(problem).font(.callout).foregroundStyle(.secondary)
                    }
                }
                .padding(.horizontal, 1)
                .padding(.vertical, 2)
            }
            .defaultScrollAnchor(.top)
            .scrollBounceBehavior(.basedOnSize)
            .scrollEdgeBar {
                Text("新規プロジェクト")
                    .font(.system(size: 21, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.bottom, 19)
            }
            .frame(maxHeight: 369)
            .fixedSize(horizontal: false, vertical: true)
            .textFieldStyle(.roundedBorder)
            .disabled(saving)

            HStack(spacing: 9) {
                Spacer()
                Button("キャンセル") { dismiss() }
                    .disabled(saving)
                    .keyboardShortcut(.cancelAction)
                Button(saving ? "作成中" : "作成", action: submit)
                    .buttonStyle(.borderedProminent)
                    .disabled(saving || cleaned(name).isEmpty || cleaned(output).isEmpty)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.top, 21)
        }
        .padding(24)
        .frame(width: 445)
        .task { nameFocused = true }
        .fileImporter(isPresented: $choosing, allowedContentTypes: [.folder], allowsMultipleSelection: folder == .documents) {
            result in
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
    }

    private func field<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
            content()
        }
    }

    private func pathField(_ label: String, placeholder: String, value: Binding<String>, target: Folder) -> some View {
        HStack(spacing: 6) {
            TextField(label, text: value, prompt: Text(placeholder))
                .labelsHidden()
            Button { choose(target) } label: {
                Image(systemName: "folder")
                    .font(.system(size: 14))
                    .frame(width: 25, height: 22)
            }
            .accessibilityLabel("\(label)のフォルダを選択")
            .help("\(label)のフォルダを選択")
        }
    }

    private func choose(_ target: Folder) {
        folder = target
        choosing = true
    }

    private func cleaned(_ value: String) -> String { value.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func submit() {
        guard !saving else { return }
        saving = true
        problem = nil
        let repository = cleaned(repo)
        let draft = ProjectDraft(name: cleaned(name), outputDir: cleaned(output),
                                 repoPath: repository.isEmpty ? nil : repository,
                                 docDirs: documents,
                                 glossary: terms.split(whereSeparator: \.isNewline).map { cleaned(String($0)) }.filter { !$0.isEmpty })
        Task {
            defer { saving = false }
            do { try await create(draft) }
            catch { problem = error.localizedDescription }
        }
    }
}
