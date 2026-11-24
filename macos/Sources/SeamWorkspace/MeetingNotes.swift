import SeamCore
import SeamUI
import SwiftUI

@MainActor
final class MeetingNotes: ObservableObject {
    struct Editor: Identifiable {
        let start: Double
        let note: MeetingNote?
        var id: Double { start }
    }

    @Published private(set) var notes: [MeetingNote] = []
    @Published var editor: Editor?
    @Published var problem: String?
    private var revision = 0

    func load(via api: SeamAPI, session: String) async {
        let requested = revision
        do {
            let loaded = try await api.meetingNotes(session: session)
            guard !Task.isCancelled, requested == revision else { return }
            notes = loaded
        } catch {
            guard !Task.isCancelled, requested == revision else { return }
            problem = error.localizedDescription
        }
    }

    func byStart(in rows: [TranscriptSegment]) -> [Double: MeetingNote] {
        var result: [Double: MeetingNote] = [:]
        for note in notes {
            guard let index = rows.transcriptIndex(at: note.start) else { continue }
            let start = rows[index].start
            if result[start] == nil || start == note.start { result[start] = note }
        }
        return result
    }

    func save(_ editing: Editor, title: String, body: String, via api: SeamAPI, session: String) async throws {
        let updated = try await api.saveMeetingNote(session: session, noteID: editing.note?.id,
                                                    start: editing.start, title: title, body: body)
        await apply(updated, via: api, session: session)
    }

    func delete(_ note: MeetingNote, via api: SeamAPI, session: String) async {
        do {
            let updated = try await api.deleteMeetingNote(session: session, id: note.id)
            await apply(updated, via: api, session: session)
        } catch {
            problem = error.localizedDescription
        }
    }

    private func apply(_ updated: MeetingNotesUpdate, via api: SeamAPI, session: String) async {
        revision += 1
        notes = updated.notes
        await load(via: api, session: session)
        if !updated.summaryQueued { problem = "メモを保存しました。要約の更新を予約できませんでした。" }
    }
}

@available(macOS 26.0, *)
struct MeetingNoteEditor: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var focusedField: Field?
    @State private var title: String
    @State private var memo: String
    @State private var showsTitle: Bool
    @State private var saving = false
    @State private var problem: String?

    private enum Field { case memo, title }

    let editing: MeetingNotes.Editor
    let save: (String, String) async throws -> Void

    init(editing: MeetingNotes.Editor, save: @escaping (String, String) async throws -> Void) {
        self.editing = editing
        self.save = save
        _title = State(initialValue: editing.note?.customTitle ?? "")
        _memo = State(initialValue: editing.note?.body ?? "")
        _showsTitle = State(initialValue: editing.note?.customTitle != nil)
    }

    private var motion: Animation? { reduceMotion ? nil : SeamMotion.update }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(editing.note == nil ? "メモを追加" : "メモを編集")
                    .font(.system(size: 22, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Label(minutesTime(editing.start), systemImage: "tag")
                    .font(.system(size: 12, weight: .medium, design: .monospaced))
                    .foregroundStyle(.secondary)
            }

            TextField("この発言にメモを書く", text: $memo, axis: .vertical)
                .font(.system(size: 14))
                .lineSpacing(5)
                .lineLimit(5, reservesSpace: true)
                .textFieldStyle(.plain)
                .focused($focusedField, equals: .memo)
                .tint(.accentColor)
                .padding(12)
                .seamTextPointer()
                .seamFieldSurface(focused: focusedField == .memo, radius: 14)

            titleRow

            GlassEffectContainer(spacing: 8) {
                HStack(spacing: 8) {
                    Spacer()
                    if saving { ProgressView().controlSize(.small) }
                    Button("キャンセル") { dismiss() }
                        .buttonStyle(SeamFormActionStyle())
                        .keyboardShortcut(.cancelAction)
                    Button("保存", systemImage: "checkmark", action: submit)
                        .buttonStyle(SeamFormActionStyle(primary: true))
                        .keyboardShortcut(.return, modifiers: .command)
                        .disabled(memo.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
                                  memo.count > 8000 || title.count > 80)
                }
            }
        }
        .padding(24)
        .frame(width: 480)
        .background(Color.seamPaper(for: colorScheme))
        .disabled(saving)
        .interactiveDismissDisabled(saving)
        .onAppear { focusedField = .memo }
        .onChange(of: showsTitle) { _, showing in
            if showing { focusedField = .title }
        }
        .alert("メモを保存できませんでした", isPresented: Binding(
            get: { problem != nil },
            set: { if !$0 { problem = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(problem ?? "")
        }
    }

    private var titleRow: some View {
        HStack(spacing: 12) {
            Text("ラベル名")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.secondary)
            if showsTitle {
                TextField("空欄ならAIが生成", text: $title)
                    .accessibilityLabel("ラベル名")
                    .font(.system(size: 13))
                    .textFieldStyle(.plain)
                    .focused($focusedField, equals: .title)
                    .tint(.accentColor)
                    .padding(.horizontal, 10)
                    .frame(height: 34)
                    .seamTextPointer()
                    .seamFieldSurface(focused: focusedField == .title)
                    .transition(reduceMotion ? .identity : .opacity)
            } else {
                Spacer(minLength: 0)
                Text("AIで生成")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                Button("ラベル名を指定", systemImage: "pencil") {
                    withAnimation(motion) { showsTitle = true }
                }
                .buttonStyle(SeamFormActionStyle())
            }
        }
        .frame(height: 36)
    }

    private func submit() {
        saving = true
        Task {
            defer { saving = false }
            do {
                try await save(title, memo)
                dismiss()
            } catch {
                problem = error.localizedDescription
            }
        }
    }

}

@available(macOS 26.0, *)
struct MeetingNotesPanel: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var notebook: MeetingNotes
    let close: () -> Void
    let jump: (Double) -> Void
    let remove: (MeetingNote) -> Void

    private var motion: Animation? { reduceMotion ? nil : SeamMotion.update }

    var body: some View {
        SeamScrollView(indicatorInset: 6) {
            LazyVStack(alignment: .leading, spacing: 4) {
                if notebook.notes.isEmpty {
                    Text("発言を右クリックしてメモを追加できます")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 12)
                }
                ForEach(notebook.notes) { note in
                    noteRow(note)
                        .transition(reduceMotion ? .identity : .offset(y: 4).combined(with: .opacity))
                }
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .animation(motion, value: notebook.notes.map(\.id))
        }
        .scrollEdgeBar {
            MeetingPanelHeader(title: "メモ・ラベル", close: close) { EmptyView() }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func noteRow(_ note: MeetingNote) -> some View {
        Button { jump(note.start) } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Label(note.title, systemImage: "tag")
                        .font(.system(size: 13, weight: .semibold))
                        .contentTransition(.opacity)
                    Spacer(minLength: 4)
                    Text(minutesTime(note.start))
                        .font(.system(size: 11, weight: .medium, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                Text(note.body)
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
                    .lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(ReaderHoverStyle(tint: .primary, motion: motion))
        .animation(motion, value: note.title)
        .contextMenu {
            MeetingNoteActions(note: note, edit: {
                notebook.editor = .init(start: note.start, note: note)
            }, remove: { remove(note) })
        }
    }
}
