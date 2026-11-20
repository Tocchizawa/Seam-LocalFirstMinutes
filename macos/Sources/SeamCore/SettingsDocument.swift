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

extension SeamAPI {
    public func settings() async throws -> SettingsDocument {
        try await request(["api", "settings"])
    }

    public func saveSettings(_ changes: SettingsDocument) async throws -> SettingsDocument {
        try await request(["api", "settings"], method: "PUT", body: JSONEncoder().encode(changes))
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

}
