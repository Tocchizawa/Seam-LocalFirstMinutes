import Foundation

public enum SettingValue: Codable, Equatable, Sendable {
    case text(String), number(Double), flag(Bool), fields([String: SettingValue]), values([SettingValue]), null

    public init(from decoder: Decoder) throws {
        let single = try decoder.singleValueContainer()
        if single.decodeNil() { self = .null }
        else if let value = try? single.decode(Bool.self) { self = .flag(value) }
        else if let value = try? single.decode(Double.self) { self = .number(value) }
        else if let value = try? single.decode(String.self) { self = .text(value) }
        else if let value = try? single.decode([String: SettingValue].self) { self = .fields(value) }
        else { self = .values(try single.decode([SettingValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var single = encoder.singleValueContainer()
        switch self {
        case .text(let text): try single.encode(text)
        case .number(let number): try single.encode(number)
        case .flag(let value): try single.encode(value)
        case .fields(let object): try single.encode(object)
        case .values(let array): try single.encode(array)
        case .null: try single.encodeNil()
        }
    }

    public var text: String? {
        if case .text(let value) = self { return value }
        return nil
    }

    public var number: Double? {
        if case .number(let value) = self { return value }
        return nil
    }

    public var flag: Bool? {
        if case .flag(let value) = self { return value }
        return nil
    }
}

public struct SettingsDocument: Codable, Equatable, Sendable {
    private var values: [String: SettingValue]

    public init(_ values: [String: SettingValue] = [:]) { self.values = values }

    public init(from decoder: Decoder) throws {
        values = try decoder.singleValueContainer().decode([String: SettingValue].self)
    }

    public func encode(to encoder: Encoder) throws {
        var single = encoder.singleValueContainer()
        try single.encode(values)
    }

    public var isEmpty: Bool { values.isEmpty }

    public subscript(path: String) -> SettingValue? {
        get {
            var current: SettingValue? = .fields(values)
            for key in path.split(separator: ".") {
                guard case .fields(let fields) = current else { return nil }
                current = fields[String(key)]
            }
            return current
        }
        set {
            let keys = path.split(separator: ".").map(String.init)
            guard !keys.isEmpty else { return }
            Self.assign(newValue, to: keys[...], in: &values)
        }
    }

    private static func assign(_ value: SettingValue?, to keys: ArraySlice<String>, in fields: inout [String: SettingValue]) {
        guard let key = keys.first else { return }
        if keys.count == 1 { fields[key] = value; return }
        var nested: [String: SettingValue]
        if case .fields(let previous) = fields[key] { nested = previous }
        else { nested = [:] }
        assign(value, to: keys.dropFirst(), in: &nested)
        fields[key] = nested.isEmpty ? nil : .fields(nested)
    }
}

public struct WhisperModels: Decodable, Sendable {
    public struct Model: Decodable, Equatable, Identifiable, Sendable {
        public let name: String
        public let label: String
        public let state: String
        public let downloaded: Bool
        public let sizeBytes: Int
        public var id: String { name }

        public init(name: String, label: String, state: String, downloaded: Bool, sizeBytes: Int = 0) {
            self.name = name
            self.label = label
            self.state = state
            self.downloaded = downloaded
            self.sizeBytes = sizeBytes
        }
    }

    public struct Download: Decodable, Equatable, Sendable {
        public let state: String
        public let model: String?
        public let percent: Double?
        public let error: String?
        public let currentBytes: Int?
        public let totalBytes: Int?

        public init(state: String, model: String?, percent: Double? = nil, error: String? = nil,
                    currentBytes: Int? = nil, totalBytes: Int? = nil) {
            self.state = state
            self.model = model
            self.percent = percent
            self.error = error
            self.currentBytes = currentBytes
            self.totalBytes = totalBytes
        }
    }

    public let models: [Model]
    public let download: Download
}

public struct CLIModel: Decodable, Identifiable, Sendable {
    public let slug: String
    public let displayName: String
    public var id: String { slug }
}

public struct ServiceConnectionCheck: Decodable, Sendable {
    public let ok: Bool
    public let message: String
    public let model: String?
}

public struct SpeakerProfile: Decodable, Identifiable, Sendable {
    public let id: String
    public let label: String
    public let sampleAvailable: Bool
    public let appearances: Int
}

public struct DiarizationStatus: Decodable, Sendable {
    public let pyannoteAvailable: Bool
    public let hasHfToken: Bool
}

extension SeamAPI {
    public func settings() async throws -> SettingsDocument {
        try await request(["api", "settings"])
    }

    public func saveSettings(_ changes: SettingsDocument) async throws -> SettingsDocument {
        try await request(["api", "settings"], method: "PUT", body: JSONEncoder().encode(changes))
    }

    public func whisperModels() async throws -> WhisperModels {
        try await request(["api", "models", "whisper"])
    }

    public func downloadWhisper(_ model: String) async throws -> WhisperModels.Download {
        struct Started: Decodable { let download: WhisperModels.Download }
        let result: Started = try await request(["api", "models", "whisper", model, "download"], method: "POST")
        return result.download
    }

    public func removeWhisper(_ model: String) async throws {
        let _: SettingsDocument = try await request(["api", "models", "whisper", model], method: "DELETE")
    }

    public func cliModels(for provider: String) async throws -> [CLIModel] {
        try await request(["api", "settings", "cli", provider, "models"])
    }

    public func defaultSummaryPrompt() async throws -> String {
        struct Prompt: Decodable { let prompt: String }
        let data: Prompt = try await request(["api", "settings", "summary", "default-prompt"])
        return data.prompt
    }

    public func configuredKeys() async throws -> [String: Bool] {
        struct Status: Decodable { let providers: [String: Bool] }
        let data: Status = try await request(["api", "summarize", "api-keys"])
        return data.providers
    }

    public func setSummaryKey(_ token: String, provider: String) async throws {
        struct Key: Encodable { let provider: String; let token: String }
        let _: SettingsDocument = try await write(["api", "summarize", "api-key"], method: "PUT",
                                                  body: Key(provider: provider, token: token))
    }

    public func removeSummaryKey(provider: String) async throws {
        let _: SettingsDocument = try await request(["api", "summarize", "api-key", provider], method: "DELETE")
    }

    public func allowSummaryProvider(_ provider: String) async throws {
        let _: SettingsDocument = try await request(["api", "summarize", "consent", provider], method: "POST")
    }

    public func checkSummaryProvider(_ provider: String, model: String?, reasoningEffort: String?) async throws -> ServiceConnectionCheck {
        struct Target: Encodable {
            let provider: String
            let model: String?
            let reasoningEffort: String?
        }
        return try await write(["api", "summarize", "test"], method: "POST",
                               body: Target(provider: provider, model: model, reasoningEffort: reasoningEffort),
                               timeout: 150)
    }

    public func diarizationStatus() async throws -> DiarizationStatus {
        try await request(["api", "speakers", "diarization", "status"])
    }

    public func setDiarizationToken(_ token: String) async throws {
        struct Secret: Encodable { let token: String }
        let _: SettingsDocument = try await write(["api", "speakers", "diarization", "token"], method: "PUT",
                                                  body: Secret(token: token))
    }

    public func removeDiarizationToken() async throws {
        let _: SettingsDocument = try await request(["api", "speakers", "diarization", "token"], method: "DELETE")
    }

    public func checkDiarization() async throws -> ServiceConnectionCheck {
        try await request(["api", "speakers", "diarization", "test"], method: "POST")
    }

    public func speakers() async throws -> [SpeakerProfile] {
        struct Result: Decodable { let speakers: [SpeakerProfile] }
        let result: Result = try await request(["api", "speakers"])
        return result.speakers
    }

    public func renameSpeaker(_ id: String, label: String) async throws {
        struct Label: Encodable { let label: String }
        let _: SettingsDocument = try await write(["api", "speakers", id], method: "PATCH", body: Label(label: label))
    }

    public func removeSpeaker(_ id: String) async throws {
        let _: SettingsDocument = try await request(["api", "speakers", id], method: "DELETE")
    }

    public func mergeSpeakers(into primaryId: String, sourceIds: [String]) async throws {
        struct Merge: Encodable { let primaryId: String; let sourceIds: [String] }
        let _: SettingsDocument = try await write(["api", "speakers", "merge"], method: "POST",
                                                  body: Merge(primaryId: primaryId, sourceIds: sourceIds))
    }

    public func speakerAudio(for id: String) throws -> URL {
        try endpoint(["api", "speakers", id, "sample"])
    }
}
