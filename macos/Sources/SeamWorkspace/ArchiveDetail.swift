import AppKit
import SeamCore
import SeamUI
import SwiftUI

/// 保存済み議事録の詳細画面。本文レイアウトは SeamUI の MinutesReader が担当する。
@available(macOS 26.0, *)
struct ArchiveDetail: View {
    private typealias Stored<Value> = SwiftUI.State<Value>

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var session: WorkspaceSession
    @StateObject private var playback = MinutesPlayback()
    @Stored private var current: Minutes?
    @Stored private var transcript: [TranscriptSegment]?
    @Stored private var transcriptLoading = false
    @Stored private var hasAudio = false
    @Stored private var summaryJob: SummaryJob?
    @Stored private var exportNotice: ExportNotice?
    @Stored private var problem: String?
    @Stored private var working = false
    @Stored private var editor: Editor?
    @Stored private var edited = ""
    @Stored private var confirmation: Confirmation?

    let item: Minutes
    let projects: [Project]
    let projectName: String
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

    private struct ExportNotice {
        let id = UUID()
        let destination: URL
    }

    private enum Confirmation: String, Identifiable {
        case delete = "この議事録を削除しますか？"
        case retry = "文字起こしを再実行しますか？"
        var id: Self { self }
    }

    private var note: Minutes { current ?? item }
    private var pipeline: PipelineStatus? { session.work.first { $0.sessionId == note.sessionId } }
    private var transcribing: Bool { pipeline?.state == "transcribing" }
    private var activeSummary: ActiveSummary? { session.summaries.first { $0.minutesId == item.id } }
    private var summarizing: Bool { activeSummary != nil || summaryJob?.active == true }

    var body: some View {
        // 本文、進行状況、操作メニューを組み合わせて詳細画面を作る。
        MinutesReader(title: note.title, date: formattedDate(note), summary: note.summary ?? "",
                      sections: transcript ?? [], transcriptLoading: transcriptLoading, query: query,
                      playback: hasAudio ? playback : nil, compact: compact, expanded: expanded,
                      showTranscript: $showTranscript,
                      toggleExpanded: compact ? nil : {
                          withAnimation(reduceMotion ? nil : SeamMotion.reposition) { expanded.toggle() }
                      },
                      backTitle: projectName, goBack: close,
                      export: export, exporting: working,
                      menu: AnyView(commands), activity: currentActivity,
                      problem: problem ?? pipeline?.error ?? summaryJob?.errorMessage,
                      exportNotice: exportNotice.map { AnyView(exportToast($0)) })
        .task(id: exportNotice?.id) {
            guard let id = exportNotice?.id else { return }
            try? await Task.sleep(for: .seconds(8))
            guard !Task.isCancelled, exportNotice?.id == id else { return }
            exportNotice = nil
        }
        .task(id: session.archiveRevision) {
            await refresh()
        }
        .task(id: "\(item.id):\(showTranscript):\(session.archiveRevision)") {
            guard showTranscript, transcript == nil else { return }
            transcriptLoading = true
            do {
                let loaded = try await session.api.transcript(id: item.id)
                guard !Task.isCancelled else { return }
                transcript = loaded
                transcriptLoading = false
            } catch {
                guard !Task.isCancelled else { return }
                transcriptLoading = false
                problem = error.localizedDescription
            }
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
                        transcript = nil
                        try await session.api.retranscribe(id: item.id)
                        session.archiveChanged()
                    }
                }
            }
        }
    }

    private var currentActivity: BackgroundActivity? {
        if transcribing, let pipeline {
            return pipeline.activity(title: note.title)
        }
        if let activeSummary { return activeSummary.activity }
        guard let summaryJob, summaryJob.active else { return nil }
        return summaryActivity(id: item.id, state: summaryJob.state, stage: summaryJob.stage, label: summaryJob.stageLabel)
    }

    // タイトル/要約の編集、再処理、削除などの詳細画面操作。
    private var commands: some View {
        Menu {
            Button("タイトルを編集…", systemImage: "pencil") { openEditor(.title, value: note.title) }
            Button("要約を編集…", systemImage: "square.and.pencil") { openEditor(.summary, value: note.summary ?? "") }
                .disabled(transcribing || summarizing)
            Divider()
            Button("議事録のMarkdownをコピー", systemImage: "document.on.document") {
                perform {
                    let markdown = try await session.api.markdown(for: item.id)
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(markdown, forType: .string)
                }
            }
            if projects.count > 1 {
                Menu("別のプロジェクトへ移動", systemImage: "folder") {
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
                Button((activeSummary?.state ?? summaryJob?.state) == "dictionary_update" ? "用語辞書の整理を中止" : "要約を中止",
                       systemImage: "stop.circle") {
                    perform {
                        try await session.cancelSummary(id: item.id)
                        summaryJob = try await session.api.summaryJob(for: item.id)
                    }
                }
            } else {
                Button(note.summary?.isEmpty == false ? "要約を再生成" : "要約を生成", systemImage: "doc.text") {
                    perform {
                        try await session.api.summarize(id: item.id)
                        summaryJob = try await session.api.summaryJob(for: item.id)
                        showTranscript = false
                    }
                }
                .disabled(transcribing)
            }
            if transcribing {
                Button("再文字起こしを中止", systemImage: "stop.circle") {
                    perform { try await session.api.cancelRetranscription(id: item.id) }
                }
            } else {
                Button("文字起こしを再実行…", systemImage: "waveform") { confirmation = .retry }
                    .disabled(!hasAudio || summarizing)
            }
            Divider()
            Button("議事録を削除…", systemImage: "trash", role: .destructive) { confirmation = .delete }
                .disabled(transcribing || summarizing)
        } label: {
            Image(systemName: "ellipsis")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(.secondary)
                .frame(width: 34, height: 34)
                .contentShape(Rectangle())
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .accessibilityLabel("その他の議事録操作")
        .help("その他の議事録操作")
        .disabled(working)
    }

    private func export() {
        perform {
            let destination = try await session.api.export(id: item.id)
            exportNotice = ExportNotice(destination: destination)
            AccessibilityNotification.Announcement("エクスポートしました").post()
        }
    }

    private func exportToast(_ notice: ExportNotice) -> some View {
        HStack(spacing: 10) {
            Label("エクスポートしました", systemImage: "checkmark.circle.fill")
                .font(.callout.weight(.medium))
            Button("Finderで開く") {
                NSWorkspace.shared.activateFileViewerSelecting([notice.destination])
                exportNotice = nil
            }
            .buttonStyle(.bordered)
            .buttonBorderShape(.capsule)
            .controlSize(.small)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .glassEffect(.regular, in: .capsule)
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
                    .overlayEditorScrollIndicators()
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
            let loaded = try await session.api.minutes(id: item.id, includeTranscript: false)
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
    formattedRecordingDate(note.startedAt) ?? note.date
}

func formattedRecordingDate(_ timestamp: String) -> String? {
    let iso = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
    let start = (try? iso.parse(timestamp)) ?? (try? Date.ISO8601FormatStyle().parse(timestamp))
    return start?.formatted(.dateTime.year().month().day().hour().minute().locale(Locale(identifier: "ja_JP")))
}
