import SeamCore
import SeamUI
import SwiftUI

@available(macOS 26.0, *)
struct MeetingPanelHeader<Controls: View>: View {
    let title: String
    var subtitle: String? = nil
    let close: () -> Void
    @ViewBuilder var controls: Controls

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                heading
                Spacer(minLength: 0)
                toolbar
            }
            VStack(alignment: .leading, spacing: 8) {
                heading
                toolbar.frame(maxWidth: .infinity, alignment: .trailing)
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 14)
        .padding(.bottom, 12)
    }

    private var heading: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.system(size: 17, weight: .semibold))
                .fixedSize(horizontal: true, vertical: false)
                .accessibilityAddTraits(.isHeader)
            if let subtitle {
                Text(verbatim: subtitle)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(idealWidth: 0, alignment: .leading)
                    .contentTransition(.opacity)
            }
        }
    }

    private var toolbar: some View {
        HStack(spacing: SeamToolbarMetrics.spacing) {
            controls
            Button(action: close) { SeamToolbarIcon("xmark") }
                .buttonStyle(SeamToolbarButtonStyle())
                .accessibilityLabel("パネルを閉じる")
                .help("パネルを閉じる")
        }
        .padding(SeamToolbarMetrics.inset)
        .seamToolbarGlass()
        .fixedSize()
    }
}

struct MeetingTranscriptQuestion: Identifiable {
    let id = UUID()
    let start: Double

    var text: String { "\(minutesTime(start))の発言では、何について話していますか？" }
}

@available(macOS 26.0, *)
struct MeetingChatPanel: View {
    let api: SeamAPI
    let session: String
    let showsQuickQuestions: Bool
    @Binding var transcriptQuestion: MeetingTranscriptQuestion?
    let jump: (Double) -> Void
    let close: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @State private var chat: MeetingConversation?
    @State private var history: [MeetingChat] = []
    @State private var input = ""
    @State private var retryQuestion: MeetingTranscriptQuestion?
    @State private var pendingQuestion: String?
    @State private var partial = ""
    @State private var working = false
    @State private var problem: String?
    @State private var renaming = false
    @State private var proposedTitle = ""
    @State private var confirmingDelete = false
    @State private var position = ScrollPosition(edge: .bottom)
    @State private var following = true
    @State private var interacting = false
    @State private var nearBottom = true
    @FocusState private var inputFocused: Bool

    private let questions = ["今どうなってる？", "決まったことは？", "TODOは？"]
    private var motion: Animation? { reduceMotion ? nil : SeamMotion.update }
    private var trimmedInput: String { input.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var canSend: Bool { !working && !trimmedInput.isEmpty && trimmedInput.count <= 8000 }

    private struct Line: Identifiable {
        let id: Int
        let role: MeetingChatMessage.Role
        let content: String
    }

    private var lines: [Line] {
        var lines = (chat?.messages ?? []).enumerated().map {
            Line(id: $0.offset, role: $0.element.role, content: $0.element.content)
        }
        if let pendingQuestion {
            lines.append(.init(id: lines.count, role: .user, content: pendingQuestion))
            lines.append(.init(id: lines.count, role: .assistant, content: partial))
        }
        return lines
    }

    private var messageTransition: AnyTransition {
        reduceMotion ? .identity : .asymmetric(
            insertion: .offset(y: 6).combined(with: .opacity),
            removal: .opacity
        )
    }

    var body: some View {
        messages
            .animatePageChange(for: chat?.id, enabled: !reduceMotion)
            .scrollEdgeBar { header }
            .scrollEdgeBar(.bottom) { composer }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .task(id: session) {
                guard chat == nil, !working else { return }
                await perform { try await loadHistory(selectLatest: true) }
                sendTranscriptQuestion()
            }
            .onChange(of: transcriptQuestion?.id) { sendTranscriptQuestion() }
            .onChange(of: working) { _, busy in
                if !busy { sendTranscriptQuestion() }
            }
            .alert("チャット名を変更", isPresented: $renaming) {
                TextField("名前", text: $proposedTitle)
                Button("キャンセル", role: .cancel) {}
                Button("保存", action: rename)
                    .disabled(proposedTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
                              proposedTitle.count > 80)
            }
            .confirmationDialog("このチャットを削除しますか？", isPresented: $confirmingDelete) {
                Button("削除", role: .destructive, action: delete)
            }
    }

    private var header: some View {
        MeetingPanelHeader(title: "会議チャット", subtitle: chat?.title, close: close) {
            HStack(spacing: SeamToolbarMetrics.spacing) {
                SeamDropdown("チャット履歴", width: 288) {
                    SeamToolbarIcon("clock.arrow.circlepath")
                } items: {
                    if history.isEmpty {
                        Text("履歴はまだありません")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .padding(8)
                    }
                    ForEach(history) { item in
                        SeamDropdownRow(item.title ?? "新規チャット", selected: item.id == chat?.id) {
                            guard item.id != chat?.id else { return }
                            Task { await perform { chat = try await api.meetingChat(session: session, id: item.id) } }
                        }
                    }
                }

                Button {
                    Task {
                        await perform {
                            chat = try await api.newMeetingChat(session: session)
                            input = ""
                            inputFocused = true
                            try await loadHistory()
                        }
                    }
                } label: { SeamToolbarIcon("square.and.pencil") }
                .buttonStyle(SeamToolbarButtonStyle())
                .accessibilityLabel("新規チャット")
                .help("新規チャット")

                SeamDropdown("チャットの操作") {
                    SeamToolbarIcon("ellipsis")
                } items: {
                    SeamDropdownRow("名前を変更…", symbol: "pencil") {
                        proposedTitle = chat?.title ?? ""
                        renaming = true
                    }
                    SeamDropdownRow("このチャットを削除…", symbol: "trash", role: .destructive) {
                        confirmingDelete = true
                    }
                }
                .disabled(chat == nil)
            }
            .fixedSize()
            .disabled(working)
        }
        .animation(motion, value: chat?.title)
    }

    private var messages: some View {
        let timeline = lines
        return SeamScrollView(indicatorInset: 6) {
            LazyVStack(alignment: .leading, spacing: 16) {
                if timeline.isEmpty {
                    Text("この会議について質問できます")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 12)
                }
                ForEach(timeline) { line in
                    message(line)
                        .transition(messageTransition)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .animation(motion, value: timeline.count)
        }
        .scrollPosition($position)
        .defaultScrollAnchor(.bottom, for: .initialOffset)
        .onScrollGeometryChange(for: Bool.self) { geometry in
            geometry.visibleRect.maxY >= geometry.contentSize.height - 32
        } action: { _, end in
            nearBottom = end
            if interacting { following = end }
        }
        .onScrollPhaseChange { _, phase in
            switch phase {
            case .interacting, .tracking, .decelerating: interacting = true
            case .idle:
                if interacting { following = nearBottom }
                interacting = false
            default: break
            }
        }
        .onChange(of: partial) {
            if following && !interacting { position.scrollTo(edge: .bottom) }
        }
        .onChange(of: chat?.id) {
            following = true
            position.scrollTo(edge: .bottom)
        }
        .onChange(of: pendingQuestion) { _, question in
            if question != nil { scrollToLatest() }
        }
        .overlay(alignment: .bottomTrailing) {
            if !following {
                Button(action: scrollToLatest) {
                    Label("最新へ", systemImage: "arrow.down")
                        .font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 12)
                        .frame(height: 32)
                }
                .buttonStyle(SeamToolbarButtonStyle())
                .glassEffect(.regular.interactive(), in: .capsule)
                .padding(.trailing, 16)
                .padding(.bottom, 8)
                .transition(messageTransition)
            }
        }
        .animation(motion, value: following)
    }

    private func scrollToLatest() {
        withAnimation(motion) {
            following = true
            position.scrollTo(edge: .bottom)
        }
    }

    @ViewBuilder
    private func message(_ line: Line) -> some View {
        if line.role == .user {
            Text(verbatim: line.content)
                .font(.system(size: 14))
                .lineSpacing(4)
                .textSelection(.enabled)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.primary.opacity(0.065), in: .rect(cornerRadius: 16))
                .padding(.leading, 28)
                .frame(maxWidth: .infinity, alignment: .trailing)
        } else {
            ZStack(alignment: .leading) {
                if line.content.isEmpty {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("回答を生成中").font(.callout)
                    }
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 4)
                    .transition(.opacity)
                } else {
                    Text(styledAnswer(line.content))
                        .font(.system(size: 14))
                        .lineSpacing(5)
                        .textSelection(.enabled)
                        .environment(\.openURL, OpenURLAction { url in
                            guard let time = citationTime(url) else { return .discarded }
                            jump(time)
                            return .handled
                        })
                        .transition(.opacity)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .animation(motion, value: line.content.isEmpty)
        }
    }

    private func styledAnswer(_ content: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        let markdown = content.replacingOccurrences(of: #"(?m)^#{1,6}[ \t]+(.+)$"#,
                                                    with: "**$1**", options: .regularExpression)
            .replacingOccurrences(of: #"(?m)^- "#, with: "• ", options: .regularExpression)
        let parsed = (try? AttributedString(markdown: markdown, options: options)) ?? AttributedString(content)
        var styled = parsed
        for (url, range) in parsed.runs[\.link] {
            if let url, citationTime(url) != nil {
                styled[range].font = .system(size: 12, weight: .semibold, design: .monospaced)
                styled[range].foregroundColor = .accentColor
            }
        }
        return styled
    }

    private func citationTime(_ url: URL) -> Double? {
        let prefix = "seam-transcript:"
        guard url.absoluteString.hasPrefix(prefix),
              let time = Double(url.absoluteString.dropFirst(prefix.count)), time.isFinite, time >= 0 else { return nil }
        return time
    }

    private var composer: some View {
        GlassEffectContainer(spacing: 8) {
            VStack(alignment: .leading, spacing: 8) {
                if let problem {
                    Label(problem, systemImage: "exclamationmark.circle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .transition(messageTransition)
                }
                if showsQuickQuestions {
                    quickQuestions
                        .disabled(working)
                        .transition(messageTransition)
                }
                inputSurface
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 16)
        }
        .animation(motion, value: canSend)
        .animation(motion, value: inputFocused)
        .animation(motion, value: showsQuickQuestions)
        .animation(motion, value: problem)
    }

    private var inputSurface: some View {
        VStack(alignment: .leading, spacing: 4) {
            TextField("この会議について聞く", text: $input, axis: .vertical)
                .font(.system(size: 14))
                .lineSpacing(4)
                .lineLimit(2...6)
                .textFieldStyle(.plain)
                .focused($inputFocused)
                .tint(.accentColor)
                .padding(.horizontal, 14)
                .padding(.top, 12)
                .background {
                    Color.clear.contentShape(Rectangle()).onTapGesture { inputFocused = true }
                }
                .seamTextPointer()

            HStack(spacing: 8) {
                Spacer()
                if inputFocused && canSend {
                    Text("⌘↵")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                        .transition(.opacity)
                }
                Button("送信", systemImage: "arrow.up") { send(input, fromComposer: true) }
                    .buttonStyle(SeamFormActionStyle(primary: true))
                    .keyboardShortcut(.return, modifiers: .command)
                    .accessibilityLabel("質問を送信")
                    .help("質問を送信（⌘Return）")
                    .disabled(!canSend)
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 8)
        }
        .background(Color.seamPaper(for: colorScheme)
            .mix(with: .primary, by: inputFocused ? 0.075 : 0.047),
            in: .rect(cornerRadius: 16))
    }

    private var quickQuestions: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 6) {
                ForEach(questions, id: \.self, content: quickQuestion)
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    ForEach(questions.prefix(2), id: \.self, content: quickQuestion)
                }
                quickQuestion(questions[2])
            }
        }
    }

    private func quickQuestion(_ question: String) -> some View {
        Button { send(question, fromComposer: false) } label: {
            Text(verbatim: question)
                .font(.system(size: 11, weight: .medium))
                .lineLimit(1)
                .fixedSize()
                .padding(.horizontal, 8)
                .frame(height: 28)
        }
        .buttonStyle(SeamToolbarButtonStyle())
        .glassEffect(.regular, in: .capsule)
    }

    private func perform(_ action: () async throws -> Void) async {
        guard !working else { return }
        working = true
        problem = nil
        defer { working = false }
        do { try await action() }
        catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func loadHistory(selectLatest: Bool = false) async throws {
        history = try await api.meetingChats(session: session)
        if selectLatest, let id = history.first?.id {
            chat = try await api.meetingChat(session: session, id: id)
        }
    }

    private func sendTranscriptQuestion() {
        guard !working, let question = transcriptQuestion else { return }
        transcriptQuestion = nil
        send(question.text, fromComposer: false, about: question)
    }

    private func send(_ text: String, fromComposer: Bool, about transcript: MeetingTranscriptQuestion? = nil) {
        let question = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !working, !question.isEmpty, question.count <= 8000 else { return }
        let selected = transcript ?? (fromComposer && retryQuestion?.text == question ? retryQuestion : nil)
        Task {
            await perform {
                if fromComposer { input = "" }
                following = true
                partial = ""
                pendingQuestion = question
                defer { pendingQuestion = nil; partial = "" }
                do {
                    if chat == nil { chat = try await api.newMeetingChat(session: session) }
                    guard let id = chat?.id else { return }
                    chat = try await api.askMeetingChat(session: session, id: id, question: question, focusStart: selected?.start) { chunk in
                        partial.append(chunk)
                    }
                    pendingQuestion = nil
                    if fromComposer { retryQuestion = nil }
                } catch {
                    if input.isEmpty {
                        input = question
                        retryQuestion = selected
                    }
                    throw error
                }
                try await loadHistory()
            }
        }
    }

    private func rename() {
        guard let id = chat?.id else { return }
        Task {
            await perform {
                chat = try await api.renameMeetingChat(session: session, id: id, title: proposedTitle)
                try await loadHistory()
            }
        }
    }

    private func delete() {
        guard let id = chat?.id else { return }
        Task {
            await perform {
                try await api.deleteMeetingChat(session: session, id: id)
                chat = nil
                try await loadHistory(selectLatest: true)
            }
        }
    }
}
