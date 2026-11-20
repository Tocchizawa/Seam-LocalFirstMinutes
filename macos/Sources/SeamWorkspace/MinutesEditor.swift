import SeamCore
import SeamUI
import SwiftUI

enum MinutesEditField: String, Identifiable {
    case title = "タイトルを編集"
    case summary = "要約を編集"

    var id: Self { self }

    func save(_ value: String, to id: String, via api: SeamAPI) async throws -> Minutes {
        switch self {
        case .title: try await api.renameMinutes(id: id, title: value)
        case .summary: try await api.updateSummary(id: id, markdown: value)
        }
    }
}

@available(macOS 26.0, *)
struct MinutesEditor: View {
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Bool
    @State private var value: String
    @State private var loaded: Bool
    @State private var saving = false
    @State private var problem: String?

    let field: MinutesEditField
    var load: (() async throws -> String)?
    let save: (String) async throws -> Void

    init(field: MinutesEditField, value: String, load: (() async throws -> String)? = nil,
         save: @escaping (String) async throws -> Void) {
        self.field = field
        self.load = load
        self.save = save
        _value = State(initialValue: value)
        _loaded = State(initialValue: load == nil)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(field.rawValue).font(.headline)
            if field == .title {
                TextField("タイトル", text: $value)
                    .focused($focused)
            } else {
                TextEditor(text: $value)
                    .overlayEditorScrollIndicators()
                    .font(.system(.body, design: .monospaced))
                    .frame(height: 270)
                    .focused($focused)
                    .disabled(!loaded || saving)
                    .accessibilityLabel("要約のMarkdown")
                    .overlay {
                        if !loaded && problem == nil { ProgressView() }
                    }
            }
            if let problem {
                Text(problem).foregroundStyle(.secondary).font(.callout)
            }
            HStack {
                Spacer()
                Button("キャンセル") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(saving)
                Button("保存", action: commit)
                    .disabled(saving || !loaded || (field == .title && value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(22)
        .frame(width: field == .title ? 420 : 550)
        .interactiveDismissDisabled(saving)
        .task {
            if let load, !loaded {
                do {
                    let text = try await load()
                    try Task.checkCancellation()
                    value = text
                    loaded = true
                } catch {
                    if !Task.isCancelled { problem = error.localizedDescription }
                }
            }
            if !Task.isCancelled && loaded { focused = true }
        }
    }

    private func commit() {
        guard !saving else { return }
        let draft = value
        saving = true
        problem = nil
        Task {
            defer { saving = false }
            do {
                try await save(draft)
                dismiss()
            } catch {
                problem = error.localizedDescription
            }
        }
    }
}
