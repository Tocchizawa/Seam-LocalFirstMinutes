import AppKit
import SeamCore
import SeamUI
import SwiftUI
import UniformTypeIdentifiers

@available(macOS 26.0, *)
public struct Workspace: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    private let pageSize = 40
    private let minimumSidebarWidth: CGFloat = 150

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.displayScale) private var displayScale
    @AppStorage("appearance") private var appearance = "system"
    @AppStorage("projectSidebarVisible") private var sidebarVisible = true
    @AppStorage("projectSidebarWidth") private var sidebarWidth: Double?
    @GestureState private var sidebarDragOffset: CGFloat = 0
    @StateObject private var session: WorkspaceSession
    private let runsSession: Bool
    @Stored private var projects: [Project] = []
    @Stored private var projectsLoaded = false
    @Stored private var pendingOrder: [String]?
    @Stored private var savingOrder = false
    @Stored private var projectID: String?
    @Stored private var projectCount: (projectID: String, count: Int)?
    @Stored private var minutes: [Minutes] = []
    @Stored private var pageFull = false
    @Stored private var loading = false
    @Stored private var loadedMinutesScope: Scope?
    @Stored private var loadError: String?
    @Stored private var page: ArchivePage = .list
    @Stored private var showTranscript = false
    @Stored private var readerExpanded = false
    @Stored private var liveExpanded = false
    @Stored private var settingsPage: NativeSettingsPage = .transcription
    @Stored private var searchOpen = false
    @Stored private var search = ""
    @Stored private var minutesEditor: MinutesEditRequest?
    @Stored private var deletingMinutes: Minutes?
    @Stored private var projectEditor: ProjectEditorPresentation?
    @Stored private var deletingProject: Project?
    @Stored private var importProject: Project?
    @Stored private var choosingAudio = false
    @Stored private var recoveringSession: String?

    public init(api: SeamAPI) {
        _session = StateObject(wrappedValue: WorkspaceSession(api: api))
        runsSession = true
    }

    package init(session: WorkspaceSession) {
        _session = StateObject(wrappedValue: session)
        runsSession = false
    }

    private struct ProjectScope: Hashable {
        let id: String?
        let revision: Int
        let connected: Bool
    }

    private struct Scope: Hashable {
        let project: ProjectScope
        let term: String
    }

    private struct ProjectEditorPresentation: Identifiable {
        let id = UUID()
        let project: Project?
    }

    private struct MinutesEditRequest: Identifiable {
        let id = UUID()
        let note: Minutes
        let field: MinutesEditField
    }

    private enum ArchivePage {
        case list
        case detail(Minutes)
        case settings

        enum Destination: Equatable {
            case list
            case detail(String)
            case settings
        }

        var destination: Destination {
            switch self {
            case .list: .list
            case .detail(let note): .detail(note.id)
            case .settings: .settings
            }
        }
    }

    private var projectScope: ProjectScope {
        ProjectScope(id: projectID, revision: session.archiveRevision, connected: session.connected)
    }
    private var scope: Scope {
        Scope(project: projectScope, term: search.trimmingCharacters(in: .whitespacesAndNewlines))
    }
    private var motion: Animation? { reduceMotion ? nil : SeamMotion.reposition }
    private var settingsOpen: Bool {
        if case .settings = page { return true }
        return false
    }
    private var selectedProject: Project? { projects.first { $0.id == projectID } }
    private var projectName: String { selectedProject?.name ?? "Seam" }
    private var showsSidebar: Bool { sidebarVisible && !readerExpanded && !liveExpanded }
    private var projectSubtitle: String? {
        guard let projectCount, projectCount.projectID == projectID else { return nil }
        return "\(projectCount.count.formatted())件の議事録"
    }
    private var errorMessage: String {
        guard let error = session.error else { return "" }
        let isScreenRecordingPermissionError = error.contains("SCREEN_CAPTURE_KIT_DISPLAY_SLEEP_PREVENTION_STARTED") ||
            error.contains("SCStreamErrorDomain") && error.contains("code=-3801")
        guard isScreenRecordingPermissionError else { return error }
        let code = error.range(of: #"code=(-?\d+)"#, options: .regularExpression)
            .map { String(error[$0].dropFirst("code=".count)) }
        return [
            "内部音声の録音には画面収録の許可が必要です。",
            "システム設定の「プライバシーとセキュリティ」から Seam の画面収録を許可して、もう一度お試しください。",
            code.map { "エラーコード: \($0)" },
        ].compactMap { $0 }.joined(separator: "\n")
    }
    private var errorTitle: String {
        session.error?.contains("SCREEN_CAPTURE_KIT_DISPLAY_SLEEP_PREVENTION_STARTED") == true ||
            session.error?.contains("SCStreamErrorDomain") == true
            ? "録音を開始できませんでした" : "処理に失敗しました"
    }

    // 画面全体の入口。録音中の分割は LiveWorkspaceSplit、文字起こし側は CapturePane で調整する。
    public var body: some View {
        LiveWorkspaceSplit(recording: session.capture != nil, expanded: liveExpanded) { compact, narrow in
            archive(compact: compact, narrow: narrow)
        } transcript: { _ in
            CapturePane(session: session, expanded: $liveExpanded)
        }
        .preferredColorScheme(appearance == "light" ? .light : (appearance == "dark" ? .dark : nil))
        .onExitCommand {
            withAnimation(motion) {
                if liveExpanded { liveExpanded = false }
                else if readerExpanded { readerExpanded = false }
            }
        }
        .onChange(of: session.capture != nil) { _, active in
            if active { readerExpanded = false }
            else { liveExpanded = false }
        }
        .task { if runsSession { await session.run() } }
        .task(id: session.connected) {
            if session.connected { await loadProjects() }
        }
        .task(id: scope) { await loadMinutes(scope) }
        .task(id: projectScope) { await loadMinutesCount(projectScope) }
        .workspaceTitleBar(projectID: projectID, title: projectName, subtitle: projectSubtitle, sidebarVisible: showsSidebar,
                           recording: session.capture != nil, toggleSidebar: toggleSidebar) {
            ActivityControl(activities: session.backgroundActivities) { activity, dismiss in
                if let job = session.summaries.first(where: { activity.id == "summary-\($0.minutesId)" }) {
                    summaryMenu(job, dismiss: dismiss)
                }
            }
            if session.capture == nil {
                RecordingStartButton(action: start)
                    .disabled(!session.connected || projectID == nil)
                    .transition(.opacity)
            }
        }
        .background(Color.seamPaper(for: colorScheme))
        .sheet(item: $projectEditor, content: projectEditorSheet)
        .sheet(item: $minutesEditor, content: minutesEditorSheet)
        .alert(errorTitle, isPresented: Binding(
            get: { session.error != nil }, set: { if !$0 { session.error = nil } }
        )) {
            Button("エラー詳細をコピー") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(session.error ?? "", forType: .string)
            }
            Button("閉じる", role: .cancel) {}
        } message: {
            Text(errorMessage)
        }
        .confirmationDialog("プロジェクトを削除しますか？", isPresented: Binding(
            get: { deletingProject != nil }, set: { if !$0 { deletingProject = nil } }
        ), presenting: deletingProject) { project in
            Button("削除", role: .destructive) { delete(project) }
        } message: { project in
            Text("「\(project.name)」とその議事録が一覧から外れます。議事録データは保存されます。")
        }
        .confirmationDialog("この議事録を削除しますか？", isPresented: Binding(
            get: { deletingMinutes != nil }, set: { if !$0 { deletingMinutes = nil } }
        ), presenting: deletingMinutes) { note in
            Button("削除", role: .destructive) {
                performMinutesAction {
                    try await session.api.deleteMinutes(id: note.id)
                    session.archiveChanged()
                }
            }
        } message: { note in
            Text(verbatim: note.title)
        }
        .fileImporter(isPresented: $choosingAudio, allowedContentTypes: [.audio, .movie, .folder]) { result in
            guard let project = importProject else { return }
            importProject = nil
            do { importAudio(try result.get(), into: project) }
            catch { session.error = error.localizedDescription }
        }
    }

    // 左のプロジェクト一覧と右側のページを並べる領域。
    private func archive(compact: Bool, narrow: Bool) -> some View {
        GeometryReader { geometry in
            let maximum = max(minimumSidebarWidth, min(480, geometry.size.width - 320))
            let preferredWidth = CGFloat(sidebarWidth ?? (narrow ? 158 : 184))
            let restingWidth = clampedSidebarWidth(preferredWidth, maximum: maximum)
            let width = clampedSidebarWidth(restingWidth + sidebarDragOffset, maximum: maximum)

            HStack(spacing: 0) {
                sidebar
                    .frame(width: width)
                    .offset(x: showsSidebar ? 0 : -width)
                    .frame(width: showsSidebar ? width : 0, alignment: .leading)
                    .clipped()
                    .overlay(alignment: .trailing) {
                        sidebarDivider(width: width, restingWidth: restingWidth, maximum: maximum)
                            .opacity(showsSidebar ? 1 : 0)
                    }
                    .allowsHitTesting(showsSidebar)
                    .accessibilityHidden(!showsSidebar)
                    .zIndex(1)

                ZStack(alignment: .topLeading) {
                    archivePage(compact: compact)
                        .transition(.identity)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .animatePageChange(for: page.destination, enabled: !reduceMotion)
            }
            .animation(motion, value: showsSidebar)
        }
    }

    private func toggleSidebar() {
        let opening = !showsSidebar
        sidebarVisible = opening
        if opening {
            readerExpanded = false
            liveExpanded = false
        }
    }

    private func clampedSidebarWidth(_ width: CGFloat, maximum: CGFloat) -> CGFloat {
        min(maximum, max(minimumSidebarWidth, width))
    }

    private func sidebarDivider(width: CGFloat, restingWidth: CGFloat, maximum: CGFloat) -> some View {
        let hitWidth: CGFloat = 10
        let lineWidth = 1 / displayScale
        return Rectangle()
            .fill(Color(nsColor: .separatorColor))
            .frame(width: lineWidth)
            .frame(width: hitWidth)
            .contentShape(Rectangle())
            .pointerStyle(.columnResize)
            .gesture(
                DragGesture(minimumDistance: 1, coordinateSpace: .global)
                    .updating($sidebarDragOffset) { value, offset, _ in
                        offset = value.translation.width
                    }
                    .onEnded { value in
                        sidebarWidth = Double(clampedSidebarWidth(restingWidth + value.translation.width,
                                                                  maximum: maximum))
                    }
            )
            .accessibilityLabel("プロジェクトパネルの幅")
            .accessibilityValue("\(Int(width))ポイント")
            .accessibilityAdjustableAction { direction in
                let change: CGFloat
                switch direction {
                case .increment: change = 16
                case .decrement: change = -16
                @unknown default: return
                }
                sidebarWidth = Double(clampedSidebarWidth(restingWidth + change, maximum: maximum))
            }
            .offset(x: (hitWidth - lineWidth) / 2)
    }

    @ViewBuilder
    // 選択中のページごとの内容。ページを増やす場合は ArchivePage とここをセットで更新する。
    private func archivePage(compact: Bool) -> some View {
        if settingsOpen {
            NativeSettings(api: session.api, page: $settingsPage, compact: compact) {
                Section("表示") {
                    SettingsRow("外観") {
                        Picker("外観", selection: $appearance) {
                            Text("システム").tag("system")
                            Text("ライト").tag("light")
                            Text("ダーク").tag("dark")
                        }
                        .labelsHidden()
                        .pickerStyle(.menu)
                        .controlSize(.large)
                        .frame(maxWidth: 245, alignment: .trailing)
                    }
                }
            }
        } else {
            ZStack {
                if case .detail(let note) = page {
                    ArchiveDetail(session: session, item: note, projects: projects, projectName: projectName, query: scope.term,
                                  compact: compact, expanded: $readerExpanded, showTranscript: $showTranscript,
                                  close: closeDetail)
                        .id(note.id)
                } else {
                    list(compact: compact)
                }
            }
            .safeAreaBar(edge: .top, spacing: 0) {
                VStack(spacing: 0) {
                    if projectID != nil, case .list = page {
                        searchHeader(compact: compact)
                            .transition(.opacity.combined(with: .offset(x: 4)))
                    }
                }
                .animation(motion, value: page.destination == .list)
            }
        }
    }

    // 左サイドバー。幅や内側の余白、項目の並びはこの関数で調整する。
    private var sidebar: some View {
        VStack(spacing: 10) {
            SeamScrollView {
                ProjectOrderList(ids: Binding(get: { projects.map(\.id) }, set: reorder)) { id in
                    if let project = projects.first(where: { $0.id == id }) {
                        projectRow(project)
                    }
                } menuItems: { id in
                    if let project = projects.first(where: { $0.id == id }) {
                        projectMenuItems(project)
                    }
                }
                .padding(.vertical, 4)
            }
            .scrollEdgeBar(blur: false) {
                Text("プロジェクト")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                    .accessibilityAddTraits(.isHeader)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.leading, 9)
                    .padding(.top, 7)
                    .padding(.bottom, 4)
            }
            NewProjectButton { openProjectEditor() }
                .disabled(!session.connected)
            Button(action: openSettings) {
                sidebarLabel("設定", symbol: "gearshape", selected: settingsOpen)
                    .frame(height: 34)
            }
            .buttonStyle(.plain)
            .keyboardShortcut(",", modifiers: .command)
            .help("設定 ⌘,")
        }
        .padding(8)
        .frame(maxHeight: .infinity)
        .background(Color.seamPanel(for: colorScheme), ignoresSafeAreaEdges: [])
    }

    private func projectRow(_ project: Project) -> some View {
        Button { select(project.id) } label: {
            sidebarLabel(project.name, symbol: "folder", selected: !settingsOpen && project.id == projectID,
                         project: project)
        }
        .buttonStyle(.plain)
    }

    private func sidebarLabel(_ title: String, symbol: String, selected: Bool, project: Project? = nil) -> some View {
        Label {
            Text(verbatim: title)
        } icon: {
            if let project {
                ProjectMark(emoji: project.emoji, color: project.color)
            } else {
                Image(systemName: symbol).frame(width: 22)
            }
        }
            .font(.system(size: 13, weight: .medium))
            .lineLimit(1)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .padding(.horizontal, 9)
            .background(Color.primary.opacity(selected ? 0.09 : 0), in: .rect(cornerRadius: 8))
            .contentShape(Rectangle())
    }

    private func searchHeader(compact: Bool) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(scope.term.isEmpty ? "議事録" : "検索結果")
                    .font(.system(size: 23, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                if !scope.term.isEmpty && !searchOpen {
                    Text(verbatim: scope.term)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 0)
            MinutesSearchField(expanded: $searchOpen, query: $search, width: compact ? 174 : 230,
                               animated: !reduceMotion)
        }
        .frame(height: 45)
        .padding(.horizontal, compact ? 14 : 33)
        .padding(.top, 22)
        .padding(.bottom, 8)
    }

    private func list(compact: Bool) -> some View {
        let workBySession = session.work.reduce(into: [String: PipelineStatus]()) { result, job in
            if let sessionId = job.sessionId { result[sessionId] = job }
        }
        let summariesByMinutes = session.summaries.reduce(into: [String: ActiveSummary]()) { result, summary in
            result[summary.minutesId] = summary
        }
        let pending = pendingRecordings

        return SeamScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                if session.recovery?.state == "running" {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.mini)
                        Text("保存されていない録音を復元中")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 8)
                }

                if let loadError {
                    archiveMessage(loadError, symbol: "exclamationmark.circle")
                } else if projectsLoaded && projectID == nil {
                    archiveMessage("新規プロジェクトを作成すると、ここに議事録が表示されます。", symbol: "folder")
                } else if loadedMinutesScope == scope && minutes.isEmpty && pending.isEmpty {
                    archiveMessage(scope.term.isEmpty ? "議事録がありません" : "一致する議事録がありません",
                                   symbol: scope.term.isEmpty ? "doc.text" : "magnifyingglass")
                }

                if !pending.isEmpty || !minutes.isEmpty {
                    LazyVStack(spacing: 0) {
                        ForEach(pending, id: \.sessionId) { job in
                            pendingRecording(job)
                                .overlay(alignment: .bottom) {
                                    if job.sessionId != pending.last?.sessionId || !minutes.isEmpty { archiveDivider }
                                }
                        }
                        ForEach(minutes) { note in
                            minutesRow(note, job: workBySession[note.sessionId], summary: summariesByMinutes[note.id])
                                .overlay(alignment: .bottom) {
                                    if note.id != minutes.last?.id { archiveDivider }
                                }
                        }
                    }
                    .padding(4)
                    .background(.primary.opacity(0.035), in: .rect(cornerRadius: 16))
                    .overlay {
                        RoundedRectangle(cornerRadius: 16)
                            .strokeBorder(.primary.opacity(0.075), lineWidth: 1 / displayScale)
                            .allowsHitTesting(false)
                    }
                }

                if pageFull && scope.term.isEmpty {
                    Button("さらに読み込む") {
                        let next = scope
                        Task { await loadMinutes(next, offset: minutes.count) }
                    }
                    .buttonStyle(.glass)
                    .disabled(loading)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 4)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, compact ? 14 : 33)
            .padding(.top, 12)
            .padding(.bottom, 24)
        }
        .scrollEdgeEffectStyle(.soft, for: .top)
    }

    private var archiveDivider: some View {
        Divider()
            .padding(.leading, 62)
            .padding(.trailing, 12)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }

    private func archiveMessage(_ message: String, symbol: String) -> some View {
        ContentUnavailableView(message, systemImage: symbol)
            .frame(maxWidth: .infinity, minHeight: 180)
    }

    private func minutesRow(_ note: Minutes, job: PipelineStatus?, summary: ActiveSummary?) -> some View {
        let activity = job.map { $0.activity(title: note.title) } ?? summary?.activity
        let snippet = note.highlights?.transcript ?? note.highlights?.summary
        return Button { open(note) } label: {
            ArchiveListRow(title: resultText(note.highlights?.title ?? note.title), date: formattedDate(note),
                           duration: note.durationSec, preview: snippet.map(resultText), activity: activity,
                           problem: job?.error, opensDetail: true)
        }
        .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
        .allowsWindowActivationEvents(true)
        .contextMenu { minutesMenu(note, job: job, summary: summary) }
    }

    private var pendingRecordings: [PipelineStatus] {
        guard let projectID, scope.term.isEmpty else { return [] }
        let displayedSessions = Set(minutes.map(\.sessionId))
        return session.work.filter { job in
            guard let sessionId = job.sessionId else { return false }
            return job.minutesId == nil && !displayedSessions.contains(sessionId)
                && job.projectId == projectID
        }
    }

    private func pendingRecording(_ job: PipelineStatus) -> some View {
        HStack(spacing: 0) {
            ArchiveListRow(title: Text("新しい録音"), date: job.startedAt.flatMap(formattedRecordingDate),
                           duration: nil, preview: nil, activity: job.activity(), problem: job.error, opensDetail: false)
            if job.state == "error", let id = job.sessionId {
                recoveryButton(id).padding(.trailing, 12)
            }
        }
    }

    private func recoveryButton(_ id: String) -> some View {
        Button(recoveringSession == id ? "復旧中" : "議事録として復旧") { recover(id) }
            .disabled(recoveringSession != nil)
    }

    @ViewBuilder
    private func minutesMenu(_ note: Minutes, job: PipelineStatus?, summary: ActiveSummary?) -> some View {
        let transcribing = job?.state == "transcribing" || job?.state == "stopping"
        let processing = transcribing || summary != nil
        Button("議事録を開く", systemImage: "doc.text") { open(note) }
        Divider()
        Button("タイトルを編集…", systemImage: "pencil") {
            minutesEditor = MinutesEditRequest(note: note, field: .title)
        }
        Button("要約を編集…", systemImage: "square.and.pencil") {
            minutesEditor = MinutesEditRequest(note: note, field: .summary)
        }
        .disabled(processing)
        if projects.contains(where: { $0.id != note.projectId }) {
            Menu("別のプロジェクトへ移動", systemImage: "folder") {
                ForEach(projects.filter { $0.id != note.projectId }) { project in
                    Button(project.name) {
                        performMinutesAction {
                            try await session.api.reassignMinutes(id: note.id, projectId: project.id)
                            session.archiveChanged()
                        }
                    }
                }
            }
            .disabled(processing)
        }
        Button("議事録のMarkdownをコピー", systemImage: "document.on.document") { copyMarkdown(note.id) }

        if summary != nil || job?.state == "error" {
            Divider()
            if let summary { cancelSummaryButton(summary) }
            if job?.state == "error", let id = job?.sessionId { recoveryButton(id) }
        }

        Divider()
        Button("議事録を削除…", systemImage: "trash", role: .destructive) { deletingMinutes = note }
            .disabled(processing)
    }

    private func minutesEditorSheet(_ request: MinutesEditRequest) -> some View {
        let loadSummary: (() async throws -> String)? = request.field == .summary ? {
            let fullNote = try await session.api.minutes(id: request.note.id, includeTranscript: false)
            return fullNote.summary ?? ""
        } : nil
        return MinutesEditor(field: request.field, value: request.field == .title ? request.note.title : "",
                             load: loadSummary) { value in
            _ = try await request.field.save(value, to: request.note.id, via: session.api)
            session.archiveChanged()
        }
    }

    @ViewBuilder
    private func summaryMenu(_ job: ActiveSummary, dismiss: @escaping () -> Void) -> some View {
        Button("議事録を開く", systemImage: "doc.text") {
            dismiss()
            performMinutesAction {
                let note = try await session.api.minutes(id: job.minutesId, includeTranscript: false)
                if note.projectId != projectID, projects.contains(where: { $0.id == note.projectId }) {
                    select(note.projectId)
                }
                open(note)
            }
        }
        Button("議事録のMarkdownをコピー", systemImage: "document.on.document") { copyMarkdown(job.minutesId) }
        Divider()
        cancelSummaryButton(job)
    }

    private func cancelSummaryButton(_ job: ActiveSummary) -> some View {
        Button(job.state == "dictionary_update" ? "用語辞書の整理を中止" : "要約を中止",
               systemImage: "stop.circle", role: .destructive) {
            performMinutesAction { try await session.cancelSummary(id: job.minutesId) }
        }
    }

    private func copyMarkdown(_ id: String) {
        performMinutesAction {
            let markdown = try await session.api.markdown(for: id)
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(markdown, forType: .string)
        }
    }

    private func performMinutesAction(_ operation: @escaping @MainActor () async throws -> Void) {
        Task {
            do { try await operation() }
            catch { session.error = error.localizedDescription }
        }
    }

    private func resultText(_ value: String) -> Text {
        var result = AttributedString()
        for (index, part) in value.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "\u{1}" || $0 == "\u{2}" }).enumerated() {
            var token = AttributedString(part)
            if index.isMultiple(of: 2) == false { token.backgroundColor = Color.primary.opacity(0.13) }
            result += token
        }
        return Text(result)
    }

    private func loadProjects() async {
        do {
            let loaded = try await session.api.projects()
            try Task.checkCancellation()
            withAnimation(motion) {
                if !savingOrder { updateProjects(loaded) }
                projectsLoaded = true
            }
        } catch {
            if !Task.isCancelled { loadError = error.localizedDescription }
        }
    }

    private func loadMinutesCount(_ requested: ProjectScope) async {
        guard requested.connected, let project = requested.id else { return }
        let count = try? await session.api.minutesCount(projectId: project)
        guard !Task.isCancelled, projectScope == requested else { return }
        projectCount = count.map { (project, $0) }
    }

    private func loadMinutes(_ requested: Scope, offset: Int = 0) async {
        guard requested.project.connected, let project = requested.project.id else { return }
        loading = true
        loadError = nil
        defer { if scope == requested { loading = false } }
        do {
            let result: [Minutes]
            if requested.term.isEmpty {
                result = try await session.api.minutes(projectId: project, limit: pageSize, offset: offset)
            } else {
                try await Task.sleep(for: .milliseconds(125))
                result = try await session.api.search(requested.term, projectId: project)
            }
            try Task.checkCancellation()
            guard scope == requested else { return }
            withAnimation(motion) {
                if offset == 0 { minutes = result }
                else {
                    let existingIDs = Set(minutes.map(\.id))
                    minutes.append(contentsOf: result.filter { !existingIDs.contains($0.id) })
                }
                loadedMinutesScope = requested
                pageFull = result.count == pageSize
            }
        } catch {
            if !Task.isCancelled && scope == requested { loadError = error.localizedDescription }
        }
    }

    private func updateProjects(_ updated: [Project]) {
        projects = updated
        guard selectedProject == nil else { return }
        if projectID == nil {
            projectID = updated.first?.id
        } else {
            select(updated.first?.id)
        }
    }

    private func select(_ id: String?) {
        if projectID != id {
            withAnimation(motion) {
                minutes = []
                loadedMinutesScope = nil
                pageFull = false
                loading = id != nil && session.connected
                loadError = nil
            }
        }
        if id == nil {
            searchOpen = false
            search = ""
        }
        projectID = id
        readerExpanded = false
        page = .list
    }

    private func open(_ note: Minutes) {
        searchOpen = false
        showTranscript = note.highlights?.transcript != nil || (note.summary?.isEmpty != false)
        page = .detail(note)
    }

    private func closeDetail() {
        guard case .detail = page else { return }
        readerExpanded = false
        page = .list
    }

    private func openSettings() {
        readerExpanded = false
        liveExpanded = false
        searchOpen = false
        search = ""
        page = .settings
    }

    private func openProjectEditor(_ project: Project? = nil) {
        guard projectEditor == nil else { return }
        projectEditor = ProjectEditorPresentation(project: project)
    }

    private func projectEditorSheet(_ editor: ProjectEditorPresentation) -> some View {
        ProjectEditor(project: editor.project, suggest: editor.project.map { project in
            { try await session.api.suggestGlossary(projectId: project.id) }
        }, cancel: { projectEditor = nil }) { draft in
            try await saveProject(draft, editing: editor)
        }
        .frame(width: 560, height: 460)
    }

    private func saveProject(_ draft: ProjectDraft, editing editor: ProjectEditorPresentation) async throws {
        let saved: Project
        if let project = editor.project {
            saved = try await session.api.updateProject(project, draft: draft)
            if let index = projects.firstIndex(where: { $0.id == saved.id }) { projects[index] = saved }
        } else {
            saved = try await session.api.createProject(draft)
            if !projects.contains(where: { $0.id == saved.id }) { updateProjects(projects + [saved]) }
        }
        guard projectEditor?.id == editor.id else { return }
        projectEditor = nil
        if editor.project == nil { select(saved.id) }
    }

    private func start() {
        guard session.connected, let projectID else { return }
        Task { await session.start(in: projectID) }
    }

    @ViewBuilder
    private func projectMenuItems(_ project: Project) -> some View {
        Button("プロジェクトを編集…") { openProjectEditor(project) }
        Button("音声・録音フォルダを取り込む…") {
            importProject = project
            choosingAudio = true
        }
        .disabled(session.importingAudioName != nil)
        Button("書き出し先を開く") { NSWorkspace.shared.open(URL(fileURLWithPath: project.outputDir)) }
        Divider()
        Button("プロジェクトを削除…", role: .destructive) { deletingProject = project }
            .disabled(session.capture != nil)
        Divider()
    }

    private func delete(_ project: Project) {
        Task {
            do {
                try await session.api.deleteProject(id: project.id)
                updateProjects(projects.filter { $0.id != project.id })
            } catch { session.error = error.localizedDescription }
        }
    }

    private func importAudio(_ url: URL, into project: Project) {
        guard session.importingAudioName == nil else { return }
        session.importingAudioName = url.lastPathComponent
        Task {
            let accessed = url.startAccessingSecurityScopedResource()
            defer {
                if accessed { url.stopAccessingSecurityScopedResource() }
                session.importingAudioName = nil
            }
            do {
                let note = try await session.api.importAudio(at: url, projectId: project.id)
                session.archiveChanged()
                if projectID == project.id, case .list = page { open(note) }
            } catch { session.error = error.localizedDescription }
        }
    }

    private func recover(_ id: String) {
        guard recoveringSession == nil else { return }
        recoveringSession = id
        Task {
            defer { recoveringSession = nil }
            do {
                try await session.api.recoverMinutes(sessionId: id)
                session.archiveChanged()
            } catch { session.error = error.localizedDescription }
        }
    }

    private func reorder(_ order: [String]) {
        let byID = Dictionary(uniqueKeysWithValues: projects.map { ($0.id, $0) })
        projects = order.compactMap { byID[$0] }
        pendingOrder = order
        guard !savingOrder else { return }
        savingOrder = true
        Task {
            defer { savingOrder = false }
            while let next = pendingOrder {
                pendingOrder = nil
                do {
                    let saved = try await session.api.reorderProjects(next)
                    if pendingOrder == nil { updateProjects(saved) }
                } catch {
                    if pendingOrder == nil, let stored = try? await session.api.projects() { updateProjects(stored) }
                    session.error = error.localizedDescription
                }
            }
        }
    }
}
