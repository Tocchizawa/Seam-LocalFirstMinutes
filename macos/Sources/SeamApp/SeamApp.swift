import AppKit
import SeamCore
import SeamUI
import SwiftUI
import UniformTypeIdentifiers

@main
struct SeamApp: App {
    @StateObject private var service = LocalBackend()
    private let port: Int? = {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: "--port"), arguments.indices.contains(index + 1),
              let value = Int(arguments[index + 1]), (1...65535).contains(value) else { return nil }
        return value
    }()

    var body: some Scene {
        Window("Seam", id: "workspace") {
            if #available(macOS 26.0, *) {
                Group {
                    if let port {
                        Workspace(api: SeamAPI(port: port))
                    } else if case .ready(let api) = service.state {
                        Workspace(api: api)
                    } else {
                        ServiceStartupView(service: service)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
                .onAppear { if port == nil { service.startOnLaunch() } }
                .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in
                    service.stop()
                }
                .frame(minWidth: 770, minHeight: 550)
            } else {
                Text("macOS 26以降が必要です").padding(40)
            }
        }
        .defaultSize(width: 1040, height: 660)
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentMinSize)
    }
}

@available(macOS 26.0, *)
private struct Workspace: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    private let pageSize = 40

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @AppStorage("appearance") private var appearance = "system"
    @StateObject private var session: WorkspaceSession
    @StateObject private var downloads: WhisperDownloads
    @Stored private var projects: [Project] = []
    @Stored private var pendingOrder: [String]?
    @Stored private var savingOrder = false
    @Stored private var projectID: String?
    @Stored private var minutes: [Minutes] = []
    @Stored private var pageFull = false
    @Stored private var loading = false
    @Stored private var loadError: String?
    @Stored private var selected: Minutes?
    @Stored private var showTranscript = false
    @Stored private var readerExpanded = false
    @Stored private var liveExpanded = false
    @Stored private var settingsOpen = false
    @Stored private var settingsPage: NativeSettingsPage = .transcription
    @Stored private var searchOpen = false
    @Stored private var search = ""
    @Stored private var chooseRecordingProject = false
    @Stored private var creatingProject = false
    @Stored private var editingProject: Project?
    @Stored private var deletingProject: Project?
    @Stored private var importProject: Project?
    @Stored private var choosingAudio = false
    @Stored private var importingAudio = false
    @Stored private var recoveringSession: String?

    init(api: SeamAPI) {
        _session = StateObject(wrappedValue: WorkspaceSession(api: api))
        _downloads = StateObject(wrappedValue: WhisperDownloads(api: api))
    }

    private struct Scope: Hashable {
        let project: String?
        let term: String
        let revision: Int
        let connected: Bool
    }

    private var scope: Scope {
        Scope(project: projectID, term: search.trimmingCharacters(in: .whitespacesAndNewlines),
              revision: session.archiveRevision, connected: session.connected)
    }
    private var motion: Animation? { reduceMotion ? nil : .smooth(duration: 0.25) }
    private var pageTransition: AnyTransition {
        guard !reduceMotion else { return .identity }
        return .asymmetric(insertion: .opacity.combined(with: .offset(y: 3)).animation(.easeOut(duration: 0.17)),
                           removal: .identity)
    }
    private var paper: Color { colorScheme == .dark ? Color(white: 0.087) : Color(white: 0.991) }
    private var accent: Color { Color(red: 0.70, green: 0.12, blue: 0.21) }
    private var projectName: String {
        projects.first { $0.id == projectID }?.name ?? "すべての議事録"
    }

    var body: some View {
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
        .task { await session.run() }
        .task { await downloads.monitor() }
        .task(id: session.connected) {
            if session.connected { await loadProjects() }
        }
        .task(id: scope) { await loadMinutes(scope) }
        .workspaceTitleBar {
            ActivityControl(downloads: downloads, activities: backgroundWork) {
                settingsPage = .transcription
                openSettings()
            }
            if session.capture == nil {
                RecordingStartButton(action: start)
                    .disabled(!session.connected || projects.isEmpty)
                    .popover(isPresented: $chooseRecordingProject, arrowEdge: .bottom) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("録音先").font(.headline)
                            ForEach(projects) { project in
                                Button(project.name) { begin(in: project.id) }
                            }
                        }
                        .padding(15)
                        .frame(minWidth: 165, alignment: .leading)
                    }
            }
        }
        .background(paper)
        .alert("操作を完了できませんでした", isPresented: Binding(
            get: { session.error != nil }, set: { if !$0 { session.error = nil } }
        )) {
            Button("閉じる", role: .cancel) {}
        } message: {
            Text(session.error ?? "")
        }
        .sheet(isPresented: $creatingProject) {
            NewProjectForm { draft in
                let project = try await session.api.createProject(draft)
                creatingProject = false
                projects.append(project)
                select(project.id)
            }
        }
        .sheet(item: $editingProject) { project in
            NewProjectForm(project: project, suggest: {
                try await session.api.suggestGlossary(projectId: project.id)
            }) { draft in
                let saved = try await session.api.updateProject(project, draft: draft)
                if let index = projects.firstIndex(where: { $0.id == saved.id }) { projects[index] = saved }
                editingProject = nil
            }
        }
        .confirmationDialog("プロジェクトを削除しますか？", isPresented: Binding(
            get: { deletingProject != nil }, set: { if !$0 { deletingProject = nil } }
        ), presenting: deletingProject) { project in
            Button("削除", role: .destructive) { delete(project) }
        } message: { project in
            Text("「\(project.name)」を一覧から削除します。議事録は「すべての議事録」から開けます。")
        }
        .fileImporter(isPresented: $choosingAudio, allowedContentTypes: [.audio, .movie, .folder]) { result in
            guard let project = importProject else { return }
            importProject = nil
            do { importAudio(try result.get(), into: project) }
            catch { session.error = error.localizedDescription }
        }
    }

    private func archive(compact: Bool, narrow: Bool) -> some View {
        HStack(spacing: 0) {
            if !readerExpanded {
                sidebar(narrow: narrow)
                    .transition(reduceMotion ? .identity : .opacity.combined(with: .offset(x: -16)))
            }

            VStack(spacing: 0) {
                if settingsOpen {
                    NativeSettings(api: session.api, downloads: downloads, page: $settingsPage, compact: compact) {
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
                    .transition(pageTransition)
                } else {
                    Group {
                        if let selected {
                            ArchiveDetail(session: session, item: selected, projects: projects, query: scope.term,
                                          compact: compact, expanded: $readerExpanded, showTranscript: $showTranscript,
                                          close: closeDetail)
                                .id(selected.id)
                                .transition(pageTransition)
                        } else {
                            list(compact: compact).transition(pageTransition)
                        }
                    }
                    .scrollEdgeBar { header(compact: compact) }
                    .transition(pageTransition)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func sidebar(narrow: Bool) -> some View {
        VStack(spacing: 10) {
            ScrollView {
                ProjectOrderList(ids: Binding(get: { projects.map(\.id) }, set: reorder)) { id in
                    if let project = projects.first(where: { $0.id == id }) {
                        projectRow(project.name, id: project.id, symbol: "folder")
                    }
                }
                .padding(.vertical, 4)
            }
            .scrollEdgeBar {
                VStack(alignment: .leading, spacing: 6) {
                    projectRow("すべての議事録", id: nil, symbol: "tray")
                        .frame(height: 34)

                    Text("プロジェクト")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.secondary)
                        .accessibilityAddTraits(.isHeader)
                        .padding(.leading, 9)
                        .padding(.top, 6)
                }
                .padding(.top, 7)
                .padding(.bottom, 4)
            }
            NewProjectButton { creatingProject = true }
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
        .frame(width: narrow ? 158 : 184)
        .background(Color.primary.opacity(0.025), in: .rect(cornerRadius: 15))
        .padding(.leading, 13)
        .padding(.vertical, 13)
    }

    private func projectRow(_ title: String, id: String?, symbol: String) -> some View {
        Button { select(id) } label: { sidebarLabel(title, symbol: symbol, selected: !settingsOpen && id == projectID) }
            .buttonStyle(.plain)
    }

    private func sidebarLabel(_ title: String, symbol: String, selected: Bool) -> some View {
        Label(title, systemImage: symbol)
            .font(.system(size: 13, weight: .medium))
            .lineLimit(1)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .padding(.horizontal, 9)
            .background(Color.primary.opacity(selected ? 0.09 : 0), in: .rect(cornerRadius: 8))
            .contentShape(Rectangle())
    }

    private func header(compact: Bool) -> some View {
        HStack(spacing: 8) {
            if selected != nil {
                Button(action: closeDetail) {
                    Label(projectName, systemImage: "chevron.left")
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(1)
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("議事録一覧に戻る")
            } else {
                Text(projectName)
                    .font(.system(size: compact ? 22 : 28, weight: .semibold))
                    .lineLimit(1)
                    .accessibilityAddTraits(.isHeader)
            }
            Spacer(minLength: 0)
            if selected == nil, let project = projects.first(where: { $0.id == projectID }) {
                projectActions(project)
            }
            MinutesSearchField(expanded: $searchOpen, query: $search, width: compact ? 174 : 230,
                               animated: !reduceMotion, onOpen: closeDetail, onEdit: closeDetail)
                .layoutPriority(1)
        }
        .frame(height: 45)
        .padding(.horizontal, compact ? 14 : 33)
        .padding(.top, 22)
        .padding(.bottom, 8)
    }

    private func list(compact: Bool) -> some View {
        ScrollView {
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
                ForEach(workForProject, id: \.sessionId) { job in
                    HStack(spacing: 8) {
                        if job.state == "transcribing" { ProgressView().controlSize(.mini) }
                        Text(job.error ?? job.stageLabel ?? job.message)
                        if job.state == "error", let id = job.sessionId {
                            Button(recoveringSession == id ? "復旧中" : "議事録として復旧") { recover(id) }
                                .disabled(recoveringSession != nil)
                        }
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 8)
                }

                if let loadError {
                    Text(loadError).font(.callout).foregroundStyle(.secondary).padding(.vertical, 12)
                } else if !session.connected {
                    Text("録音サービスに接続中").font(.callout).foregroundStyle(.secondary).padding(.vertical, 12)
                } else if loading && minutes.isEmpty {
                    ProgressView().controlSize(.small).padding(.vertical, 12)
                } else if minutes.isEmpty && workForProject.isEmpty {
                    Text(scope.term.isEmpty ? "議事録がありません" : "一致する議事録がありません")
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 12)
                }

                ForEach(minutes) { note in
                    Button(action: { open(note) }) {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                resultText(note.highlights?.title ?? note.title)
                                    .font(.system(size: 15, weight: .semibold))
                                    .lineLimit(2)
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
                    .buttonStyle(.plain)
                    .overlay(alignment: .bottom) {
                        Rectangle().fill(Color.primary.opacity(0.065)).frame(height: 1)
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

    private var workForProject: [PipelineStatus] {
        session.work.filter { job in
            job.sessionId != nil && (projectID == nil || job.projectId == projectID)
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
        for job in session.work where job.state == "transcribing" {
            guard let id = job.sessionId else { continue }
            let detail = projects.first { $0.id == job.projectId }?.name
            active.append(.init(id: "transcribe-\(id)", name: job.stageLabel ?? "文字起こし中",
                                detail: detail, symbol: "waveform", progress: job.progress))
        }
        for job in session.summaries {
            let label = switch job.state {
            case "queued": "要約を待機中"
            case "dictionary_update": "用語辞書を整理中"
            default: "要約中"
            }
            let detail = minutes.first { $0.id == job.minutesId }?.title ?? projects.first { $0.id == job.projectId }?.name
            active.append(.init(id: "summary-\(job.minutesId)", name: label, detail: detail, symbol: "doc.text"))
        }
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
            if !savingOrder { projects = loaded }
        } catch {
            if !Task.isCancelled { loadError = error.localizedDescription }
        }
    }

    private func loadMinutes(_ requested: Scope, offset: Int = 0) async {
        guard requested.connected else { return }
        loading = true
        loadError = nil
        defer { if scope == requested { loading = false } }
        do {
            let result: [Minutes]
            if requested.term.isEmpty {
                result = try await session.api.minutes(projectId: requested.project, limit: pageSize, offset: offset)
            } else {
                try await Task.sleep(for: .milliseconds(125))
                result = try await session.api.search(requested.term, projectId: requested.project)
            }
            try Task.checkCancellation()
            guard scope == requested else { return }
            if offset == 0 { minutes = result }
            else { minutes.append(contentsOf: result.filter { note in !minutes.contains { $0.id == note.id } }) }
            pageFull = result.count == pageSize
        } catch {
            if !Task.isCancelled && scope == requested { loadError = error.localizedDescription }
        }
    }

    private func select(_ id: String?) {
        withAnimation(motion) {
            if projectID != id { minutes = []; pageFull = false }
            settingsOpen = false
            closeDetail()
            projectID = id
        }
    }

    private func open(_ note: Minutes) {
        withAnimation(motion) {
            showTranscript = note.highlights?.transcript != nil || (note.summary?.isEmpty != false)
            selected = note
        }
    }

    private func closeDetail() {
        withAnimation(motion) {
            selected = nil
            readerExpanded = false
        }
    }

    private func openSettings() {
        withAnimation(motion) {
            closeDetail()
            liveExpanded = false
            searchOpen = false
            search = ""
            settingsOpen = true
        }
    }

    private func start() {
        if let projectID { begin(in: projectID) }
        else if projects.count == 1, let first = projects.first { begin(in: first.id) }
        else { chooseRecordingProject = true }
    }

    private func begin(in project: String) {
        chooseRecordingProject = false
        Task { await session.start(in: project) }
    }

    private func projectActions(_ project: Project) -> some View {
        Menu("プロジェクトの操作", systemImage: "ellipsis.circle") {
            Button("音声・録音フォルダを取り込む…") {
                importProject = project
                choosingAudio = true
            }
            .disabled(importingAudio)
            Button("プロジェクトを編集…") { editingProject = project }
            Button("書き出し先を開く") { NSWorkspace.shared.open(URL(fileURLWithPath: project.outputDir)) }
            Divider()
            Button("プロジェクトを削除…", role: .destructive) { deletingProject = project }
                .disabled(session.capture != nil)
        }
        .labelStyle(.iconOnly)
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .accessibilityLabel("プロジェクトの操作")
        .help("プロジェクトの操作")
    }

    private func delete(_ project: Project) {
        Task {
            do {
                try await session.api.deleteProject(id: project.id)
                projects.removeAll { $0.id == project.id }
                if projectID == project.id { select(nil) }
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
                if projectID == project.id, !settingsOpen { open(note) }
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
                    if pendingOrder == nil { projects = saved }
                } catch {
                    if pendingOrder == nil, let stored = try? await session.api.projects() { projects = stored }
                    session.error = error.localizedDescription
                }
            }
        }
    }
}
