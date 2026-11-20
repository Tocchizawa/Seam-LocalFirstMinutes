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
    @Stored private var projectEditor: ProjectEditorPresentation?
    @Stored private var deletingProject: Project?
    @Stored private var importProject: Project?
    @Stored private var choosingAudio = false
    @Stored private var importingAudio = false
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
    private var accent: Color { Color(red: 0.70, green: 0.12, blue: 0.21) }
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
        .workspaceTitleBar(title: projectName, subtitle: projectSubtitle, sidebarVisible: showsSidebar,
                           recording: session.capture != nil, toggleSidebar: toggleSidebar) {
            ActivityControl(activities: backgroundWork) { activity, dismiss in
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
            .scrollEdgeBar {
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
        MinutesSearchField(expanded: $searchOpen, query: $search, width: compact ? 174 : 230,
                           animated: !reduceMotion, onOpen: closeDetail, onEdit: closeDetail)
            .frame(maxWidth: .infinity, alignment: .trailing)
            .frame(height: 45)
            .padding(.horizontal, compact ? 14 : 33)
            .padding(.top, 22)
            .padding(.bottom, 8)
    }

    // 議事録一覧。行の見た目は pendingRecording とこの下の行構成を参照する。
    private func list(compact: Bool) -> some View {
        SeamScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                if session.recovery?.state == "running" {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.mini)
                        Text("保存されていない録音を復元中")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 10)
                }
                ForEach(pendingRecordings, id: \.sessionId) { job in
                    pendingRecording(job)
                }

                if let loadError {
                    Text(loadError).font(.callout).foregroundStyle(.secondary).padding(.vertical, 12)
                } else if projectsLoaded && projectID == nil {
                    Text("新規プロジェクトを作成すると、ここに議事録が表示されます。")
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 12)
                } else if loadedMinutesScope == scope && minutes.isEmpty && pendingRecordings.isEmpty {
                    Text(scope.term.isEmpty ? "議事録がありません" : "一致する議事録がありません")
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 12)
                }

                ForEach(minutes) { note in
                    let job = session.work.first { $0.sessionId == note.sessionId }
                    let summary = session.summaries.first { $0.minutesId == note.id }
                    let activity = job.map(recordingActivity) ?? summary.map(summaryActivity)
                    let failed = job?.state == "error"
                    Button(action: { open(note) }) {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(spacing: 10) {
                                rowTitle(resultText(note.highlights?.title ?? note.title), activity: activity,
                                         failure: failed ? job?.error : nil)
                                Spacer(minLength: 0)
                                Text(verbatim: formattedDate(note))
                                    .font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                                    .fixedSize()
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 10, weight: .semibold))
                                    .foregroundStyle(.tertiary)
                            }
                            if let snippet = note.highlights?.transcript ?? note.highlights?.summary {
                                resultText(snippet)
                                    .font(.system(size: 12))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(2)
                            }
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 7)
                        .frame(minHeight: 54)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
                    .accessibilityValue(activity.map(statusText) ?? "")
                    .overlay(alignment: .bottom) {
                        Rectangle().fill(Color.primary.opacity(0.065)).frame(height: 1)
                    }
                    .contextMenu {
                        if let summary { summaryMenu(summary, dismiss: {}) }
                        if let job, failed, let id = job.sessionId { recoveryButton(id) }
                    }
                }

                if pageFull && scope.term.isEmpty {
                    Button("さらに読み込む") {
                        let next = scope
                        Task { await loadMinutes(next, offset: minutes.count) }
                    }
                    .disabled(loading)
                    .padding(.vertical, 12)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, compact ? 14 : 33)
            .padding(.bottom, 24)
        }
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
        let activity = recordingActivity(job)
        return HStack(spacing: 10) {
            rowTitle(Text("新しい録音"), activity: activity, failure: job.error)
                .accessibilityElement(children: .combine)
                .accessibilityValue(statusText(activity))
            Spacer(minLength: 0)
            if let date = job.startedAt.flatMap(formattedRecordingDate) {
                Text(verbatim: date).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            if job.state == "error", let id = job.sessionId { recoveryButton(id) }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .frame(minHeight: 54)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Color.primary.opacity(0.065)).frame(height: 1)
        }
    }

    private func recoveryButton(_ id: String) -> some View {
        Button(recoveringSession == id ? "復旧中" : "議事録として復旧") { recover(id) }
            .disabled(recoveringSession != nil)
    }

    private func rowTitle(_ title: Text, activity: BackgroundActivity?, failure: String?) -> some View {
        HStack(spacing: 8) {
            title.font(.system(size: 15, weight: .semibold)).lineLimit(1)
            if let activity {
                Group {
                    if activity.symbol == "exclamationmark.circle" {
                        Image(systemName: activity.symbol).font(.caption).foregroundStyle(.orange)
                    } else if reduceMotion {
                        Image(systemName: "clock").font(.caption).foregroundStyle(.secondary)
                    } else {
                        ProgressView().controlSize(.mini).tint(.secondary)
                    }
                }
                .frame(width: 14, height: 14)
                .accessibilityHidden(true)
                .help(failure ?? statusText(activity))
            }
        }
    }

    private func statusText(_ activity: BackgroundActivity) -> String {
        guard let progress = activity.progress else { return activity.name }
        return activity.name + " " + progress.formatted(.percent.precision(.fractionLength(0)))
    }

    private func recordingActivity(_ job: PipelineStatus) -> BackgroundActivity {
        let title = job.minutesTitle ?? minutes.first { $0.sessionId == job.sessionId }?.title
        let detail = title ?? projects.first { $0.id == job.projectId }?.name
        let label: String
        switch job.state {
        case "error": label = "文字起こしに失敗"
        case "stopping": label = "録音を保存中"
        default: label = job.stageLabel ?? "文字起こし中"
        }
        return .init(id: "transcribe-\(job.sessionId ?? "")", name: label, detail: detail,
                     symbol: job.state == "error" ? "exclamationmark.circle" : "waveform", progress: job.progress)
    }

    private func summaryActivity(_ job: ActiveSummary) -> BackgroundActivity {
        let label = switch job.state {
        case "queued": "要約を待機中"
        case "dictionary_update": "用語辞書を整理中"
        default: "要約中"
        }
        let detail = minutes.first { $0.id == job.minutesId }?.title ?? job.title ?? projects.first { $0.id == job.projectId }?.name
        return .init(id: "summary-\(job.minutesId)", name: label, detail: detail, symbol: "doc.text")
    }

    @ViewBuilder
    private func summaryMenu(_ job: ActiveSummary, dismiss: @escaping () -> Void) -> some View {
        Button("議事録を開く", systemImage: "doc.text") {
            dismiss()
            performSummaryAction {
                let note = try await session.api.minutes(id: job.minutesId)
                if note.projectId != projectID, projects.contains(where: { $0.id == note.projectId }) {
                    select(note.projectId)
                }
                open(note)
            }
        }
        Button("議事録のMarkdownをコピー", systemImage: "document.on.document") {
            performSummaryAction {
                let markdown = try await session.api.markdown(for: job.minutesId)
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(markdown, forType: .string)
            }
        }
        Divider()
        Button(job.state == "dictionary_update" ? "用語辞書の整理を中止" : "要約を中止",
               systemImage: "stop.circle", role: .destructive) {
            performSummaryAction { try await session.cancelSummary(id: job.minutesId) }
        }
    }

    private func performSummaryAction(_ operation: @escaping @MainActor () async throws -> Void) {
        Task {
            do { try await operation() }
            catch { session.error = error.localizedDescription }
        }
    }

    private var backgroundWork: [BackgroundActivity] {
        var active: [BackgroundActivity] = []
        if importingAudio {
            active.append(.init(id: "import", name: "音声を取り込み中", symbol: "waveform"))
        }
        if session.recovery?.state == "running" {
            active.append(.init(id: "restore", name: "録音を復元中", detail: session.recovery?.item?.date, symbol: "arrow.trianglehead.clockwise"))
        }
        active += session.work.filter { $0.sessionId != nil && $0.state != "error" }.map(recordingActivity)
        active += session.summaries.map(summaryActivity)
        return active
    }

    private func resultText(_ value: String) -> Text {
        var result = AttributedString()
        for (index, part) in value.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "\u{1}" || $0 == "\u{2}" }).enumerated() {
            var token = AttributedString(part)
            if index.isMultiple(of: 2) == false { token.backgroundColor = accent.opacity(0.13) }
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
                else { minutes.append(contentsOf: result.filter { note in !minutes.contains { $0.id == note.id } }) }
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
        .disabled(importingAudio)
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
        guard !importingAudio else { return }
        importingAudio = true
        Task {
            let accessed = url.startAccessingSecurityScopedResource()
            defer {
                if accessed { url.stopAccessingSecurityScopedResource() }
                importingAudio = false
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
