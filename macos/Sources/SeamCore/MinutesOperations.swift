import Foundation

public struct ProjectDraft: Encodable, Sendable {
    public let name: String
    public let outputDir: String
    public let emoji: String?
    public let color: String?
    public let repoPath: String?
    public let docDirs: [String]
    public let glossary: [String]
    public let corrections: [ProjectCorrection]?

    public init(name: String, outputDir: String, repoPath: String? = nil, docDirs: [String] = [], glossary: [String] = [],
                corrections: [ProjectCorrection]? = nil, emoji: String? = nil, color: String? = nil) {
        self.name = name
        self.outputDir = outputDir
        self.repoPath = repoPath
        self.docDirs = docDirs
        self.glossary = glossary
        self.corrections = corrections
        self.emoji = emoji
        self.color = color
    }
}

public struct SummaryJob: Decodable, Sendable {
    public let state: String
    public let stage: String?
    public let stageLabel: String?
    public let errorMessage: String?

    public var active: Bool {
        ["queued", "running", "dictionary_update"].contains(state)
    }
}

public struct ActiveSummary: Decodable, Equatable, Sendable {
    public let minutesId: String
    public let projectId: String?
    public let title: String?
    public let state: String?
    public let stage: String?
    public let stageLabel: String?
}

extension SeamAPI {
    private struct Receipt: Decodable {}

    public func createProject(_ draft: ProjectDraft) async throws -> Project {
        try await write(["api", "projects"], method: "POST", body: draft)
    }

    public func updateProject(_ previous: Project, draft: ProjectDraft) async throws -> Project {
        var fields = SettingsDocument()
        if draft.name != previous.name { fields["name"] = .text(draft.name) }
        if draft.outputDir != previous.outputDir { fields["output_dir"] = .text(draft.outputDir) }
        if draft.emoji != previous.emoji { fields["emoji"] = draft.emoji.map(SettingValue.text) ?? .null }
        if draft.color != previous.color { fields["color"] = draft.color.map(SettingValue.text) ?? .null }
        if draft.repoPath != previous.repoPath { fields["repo_path"] = draft.repoPath.map(SettingValue.text) ?? .null }
        if draft.docDirs != previous.docDirs ?? [] { fields["doc_dirs"] = .values(draft.docDirs.map(SettingValue.text)) }
        if draft.glossary != previous.glossary ?? [] { fields["glossary"] = .values(draft.glossary.map(SettingValue.text)) }
        if let corrections = draft.corrections, corrections != previous.corrections ?? [] {
            fields["corrections"] = .values(corrections.map { .fields(["wrong": .text($0.wrong), "correct": .text($0.correct)]) })
        }
        guard !fields.isEmpty else { return previous }
        return try await write(["api", "projects", previous.id], method: "PUT", body: fields)
    }

    public func deleteProject(id: String) async throws {
        let _: Receipt = try await request(["api", "projects", id], method: "DELETE")
    }

    public func importAudio(at url: URL, projectId: String) async throws -> Minutes {
        struct Import: Encodable { let projectId: String; let sourcePath: String }
        struct Imported: Decodable { let minutes: Minutes }
        let result: Imported = try await write(["api", "minutes", "import-audio"], method: "POST",
                                             body: Import(projectId: projectId, sourcePath: url.path), timeout: 240)
        return result.minutes
    }

    public func recoverMinutes(sessionId: String) async throws {
        let _: Receipt = try await request(["api", "recording", "sessions", sessionId, "recover"],
                                          method: "POST", timeout: 240)
    }

    public func suggestGlossary(projectId: String) async throws -> [String] {
        struct Job: Decodable {
            struct Suggestion: Decodable { let term: String }
            struct Problem: Decodable { let message: String }
            let jobId: String
            let state: String
            let suggestions: [Suggestion]?
            let error: Problem?
        }
        let path = ["api", "projects", projectId, "glossary", "auto-generate"]
        let started: Job = try await request(path, method: "POST")
        while true {
            try await Task.sleep(for: .seconds(1))
            let job: Job = try await request(path + [started.jobId])
            switch job.state {
            case "done": return job.suggestions?.map(\.term) ?? []
            case "error": throw BackendError(status: 0, code: nil, message: job.error?.message ?? "用語集の抽出に失敗しました。")
            default: continue
            }
        }
    }

    public func audioAvailable(in sessionId: String) async throws -> Bool {
        guard !sessionId.isEmpty else { return false }
        do {
            let _: Receipt = try await request(["api", "recording", "sessions", sessionId, "audio_info"])
            return true
        } catch let error as BackendError where error.status == 404 {
            return false
        }
    }

    public func markdown(for id: String) async throws -> String {
        struct Markdown: Decodable { let content: String }
        let result: Markdown = try await request(["api", "minutes", id, "markdown"])
        return result.content
    }

    public func export(id: String) async throws -> URL {
        struct Export: Decodable { let path: String }
        let result: Export = try await request(["api", "minutes", id, "export"], method: "POST")
        return URL(fileURLWithPath: result.path)
    }

    public func renameMinutes(id: String, title: String) async throws -> Minutes {
        struct Title: Encodable { let title: String }
        return try await write(["api", "minutes", id], method: "PATCH", body: Title(title: title))
    }

    public func updateSummary(id: String, markdown: String) async throws -> Minutes {
        struct Summary: Encodable { let summary: String }
        return try await write(["api", "minutes", id, "summary"], method: "PATCH", body: Summary(summary: markdown))
    }

    public func deleteMinutes(id: String) async throws {
        let _: Receipt = try await request(["api", "minutes", id], method: "DELETE")
    }

    public func reassignMinutes(id: String, projectId: String) async throws {
        struct ProjectAssignment: Encodable { let projectId: String }
        let _: Receipt = try await write(["api", "minutes", id, "project"], method: "PUT",
                                         body: ProjectAssignment(projectId: projectId))
    }

    public func summaryJob(for id: String) async throws -> SummaryJob {
        try await request(["api", "minutes", id, "summarize", "status"])
    }

    public func activeSummaries() async throws -> [ActiveSummary] {
        try await request(["api", "summarize", "active"])
    }

    public func summarize(id: String) async throws {
        let _: Receipt = try await request(["api", "minutes", id, "summarize"], method: "POST")
    }

    public func cancelSummary(id: String) async throws {
        let _: Receipt = try await request(["api", "minutes", id, "summarize", "cancel"], method: "POST")
    }

    public func retranscribe(id: String) async throws {
        let _: Receipt = try await request(["api", "minutes", id, "retranscribe"], method: "POST")
    }

    public func cancelRetranscription(id: String) async throws {
        let _: Receipt = try await request(["api", "minutes", id, "retranscribe", "cancel"], method: "POST")
    }
}
