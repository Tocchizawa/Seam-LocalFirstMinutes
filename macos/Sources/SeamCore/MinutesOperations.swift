import Foundation

public struct ProjectDraft: Encodable, Sendable {
    public let name: String
    public let outputDir: String
    public let repoPath: String?
    public let docDirs: [String]
    public let glossary: [String]

    public init(name: String, outputDir: String, repoPath: String? = nil, docDirs: [String] = [], glossary: [String] = []) {
        self.name = name
        self.outputDir = outputDir
        self.repoPath = repoPath
        self.docDirs = docDirs
        self.glossary = glossary
    }
}

public struct SummaryJob: Decodable, Sendable {
    public let state: String
    public let stageLabel: String?
    public let errorMessage: String?

    public var active: Bool {
        ["queued", "running", "dictionary_update"].contains(state)
    }
}

public struct ActiveSummary: Decodable, Equatable, Sendable {
    public let minutesId: String
    public let projectId: String?
    public let state: String?
}

extension SeamAPI {
    private struct Receipt: Decodable {}

    public func createProject(_ draft: ProjectDraft) async throws -> Project {
        try await write(["api", "projects"], method: "POST", body: draft)
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
