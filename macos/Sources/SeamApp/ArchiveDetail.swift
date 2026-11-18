import AppKit
import SeamCore
import SeamUI
import SwiftUI

@available(macOS 26.0, *)
struct ArchiveDetail: View {
    private typealias Stored<Value> = SwiftUI.State<Value>

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var session: WorkspaceSession
    @StateObject private var playback = MinutesPlayback()
    @Stored private var current: Minutes?
    @Stored private var hasAudio = false
    @Stored private var summaryJob: SummaryJob?
    @Stored private var problem: String?
    @Stored private var working = false
    @Stored private var editor: Editor?
    @Stored private var edited = ""
    @Stored private var confirmation: Confirmation?

    let item: Minutes
    let projects: [Project]
    let query: String
    let compact: Bool
    @Binding var expanded: Bool
    @Binding var showTranscript: Bool
    let close: () -> Void

    private enum Editor: String, Identifiable {
        case title = "タイトルを編集"
        case summary = "要約を編集"
        var id: Self { self }
    }

    private enum Confirmation: String, Identifiable {
        case delete = "この議事録を削除しますか？"
        case retry = "文字起こしを再実行しますか？"
        var id: Self { self }
    }

    private var note: Minutes { current ?? item }
    private var pipeline: PipelineStatus? { session.work.first { $0.sessionId == note.sessionId } }
    private var transcribing: Bool { pipeline?.state == "transcribing" }
    private var summarizing: Bool { summaryJob?.active == true }

    var body: some View {
        VStack(spacing: 0) {
            if transcribing, let pipeline {
                activity(pipeline.stageLabel ?? pipeline.message, progress: pipeline.progress)
            } else if let summaryJob, summaryJob.active {
                activity(summaryJob.stageLabel ?? "要約を処理中")
            } else if let error = problem ?? pipeline?.error ?? summaryJob?.errorMessage {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, compact ? 14 : 33)
                    .padding(.vertical, 8)
            }

            MinutesReader(title: note.title, date: formattedDate(note), summary: note.summary ?? "",
                          sections: note.transcript ?? [], query: query,
                          playback: hasAudio ? playback : nil, compact: compact, expanded: expanded,
                          showTranscript: $showTranscript,
                          toggleExpanded: compact ? nil : {
                              withAnimation(reduceMotion ? nil : .smooth(duration: 0.25)) { expanded.toggle() }
                          })
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) { commands }
        }
        .task(id: session.archiveRevision) {
            await refresh()
        }
        .task { await watchSummary() }
        .onDisappear { playback.pause() }
        .sheet(item: $editor) { field in editorSheet(field) }
        .alert(confirmation?.rawValue ?? "", isPresented: Binding(
            get: { confirmation != nil },
            set: { if !$0 { confirmation = nil } }
        ), presenting: confirmation) { mode in
            Button("キャンセル", role: .cancel) {}
            if mode == .delete {
                Button("削除", role: .destructive) {
                    perform {
                        try await session.api.deleteMinutes(id: item.id)
                        session.archiveChanged()
                        close()
                    }
                }
            } else {
                Button("再実行") {
                    perform {
                        playback.pause()
                        try await session.api.retranscribe(id: item.id)
                        session.archiveChanged()
                    }
                }
            }
        }
    }

    private var commands: some View {
        Menu("議事録の操作", systemImage: "ellipsis") {
            Button("タイトルを編集") { openEditor(.title, value: note.title) }
            Button("要約を編集") { openEditor(.summary, value: note.summary ?? "") }
                .disabled(transcribing || summarizing)
            Divider()
            Button("Markdownをコピー") {
                perform {
                    let markdown = try await session.api.markdown(for: item.id)
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(markdown, forType: .string)
                }
            }
            Button("ファイルに書き出す") {
                perform {
                    let destination = try await session.api.export(id: item.id)
                    NSWorkspace.shared.activateFileViewerSelecting([destination])
                }
            }
            if projects.count > 1 {
                Menu("別のプロジェクトへ移動") {
                    ForEach(projects.filter { $0.id != note.projectId }) { project in
                        Button(project.name) {
                            perform {
                                try await session.api.reassignMinutes(id: item.id, projectId: project.id)
                                session.archiveChanged()
                            }
                        }
                    }
                }
            }
            Divider()
            if summarizing {
                Button("要約を中止") { perform { try await session.api.cancelSummary(id: item.id) } }
            } else {
                Button(note.summary?.isEmpty == false ? "要約を再生成" : "要約を生成") {
                    perform {
                        try await session.api.summarize(id: item.id)
                        summaryJob = try await session.api.summaryJob(for: item.id)
                        showTranscript = false
                    }
                }
                .disabled(transcribing)
            }
            if transcribing {
                Button("再文字起こしを中止") {
                    perform { try await session.api.cancelRetranscription(id: item.id) }
                }
            } else {
                Button("文字起こしを再実行") { confirmation = .retry }
                    .disabled(!hasAudio || summarizing)
            }
            Button("議事録を削除", role: .destructive) { confirmation = .delete }
                .disabled(transcribing || summarizing)
        }
        .menuStyle(.borderlessButton)
        .labelStyle(.iconOnly)
        .accessibilityLabel("議事録の操作")
        .disabled(working)
    }

    private func activity(_ text: String, progress: Double? = nil) -> some View {
        HStack(spacing: 8) {
            if let progress, progress > 0 {
                ProgressView(value: min(progress, 1)).frame(width: 54)
            } else {
                ProgressView().controlSize(.mini)
            }
            Text(verbatim: text)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, compact ? 14 : 33)
        .padding(.vertical, 6)
    }

    private func openEditor(_ field: Editor, value: String) {
        edited = value
        editor = field
    }

    private func editorSheet(_ field: Editor) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(field.rawValue).font(.headline)
            if field == .title {
                TextField("タイトル", text: $edited)
            } else {
                TextEditor(text: $edited)
                    .font(.system(.body, design: .monospaced))
                    .frame(minHeight: 270)
                    .accessibilityLabel("要約のMarkdown")
            }
            HStack {
                Spacer()
                Button("キャンセル") { editor = nil }.keyboardShortcut(.cancelAction)
                Button("保存") {
                    let value = edited
                    perform {
                        if field == .title {
                            current = try await session.api.renameMinutes(id: item.id, title: value)
                        } else {
                            current = try await session.api.updateSummary(id: item.id, markdown: value)
                        }
                        editor = nil
                        session.archiveChanged()
                    }
                }
                .disabled(working || (field == .title && edited.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
                .keyboardShortcut(.defaultAction)
            }
            if let problem { Text(problem).foregroundStyle(.secondary).font(.callout) }
        }
        .padding(22)
        .frame(minWidth: 420)
    }

    private func perform(_ operation: @escaping @MainActor () async throws -> Void) {
        guard !working else { return }
        working = true
        problem = nil
        Task {
            defer { working = false }
            do { try await operation() }
            catch { problem = error.localizedDescription }
        }
    }

    private func refresh() async {
        do {
            let loaded = try await session.api.minutes(id: item.id)
            try Task.checkCancellation()
            current = loaded
            let audio = try await session.api.audioAvailable(in: loaded.sessionId)
            try Task.checkCancellation()
            if audio {
                playback.load(id: loaded.sessionId, url: try session.api.audioURL(for: loaded.sessionId),
                              durationHint: loaded.durationSec)
            } else {
                playback.clear()
            }
            hasAudio = audio
        } catch is CancellationError {
        } catch {
            guard !Task.isCancelled else { return }
            problem = error.localizedDescription
        }
    }

    private func watchSummary() async {
        while !Task.isCancelled {
            let wasActive = summaryJob?.active == true
            if let job = try? await session.api.summaryJob(for: item.id) {
                guard !Task.isCancelled else { return }
                summaryJob = job
                if wasActive && !job.active { await refresh() }
            }
            do { try await Task.sleep(for: .seconds(wasActive ? 1.5 : 4)) }
            catch { return }
        }
    }
}

func formattedDate(_ note: Minutes) -> String {
    let iso = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
    let start = (try? iso.parse(note.startedAt)) ?? (try? Date.ISO8601FormatStyle().parse(note.startedAt))
    return start?.formatted(.dateTime.year().month().day().hour().minute().locale(Locale(identifier: "ja_JP"))) ?? note.date
}
