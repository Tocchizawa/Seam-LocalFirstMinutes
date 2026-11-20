import Foundation

public struct BackendError: LocalizedError, Sendable {
    public let status: Int
    public let code: String?
    public let message: String

    public var errorDescription: String? { message }
}

/// Accesses the existing FastAPI service on loopback. Requests that change state
/// are attempted once so a lost response cannot silently repeat an operation.
public struct SeamAPI: Sendable {
    // Keep the event stream on a dedicated session with Foundation's default
    // resource timeout so it can stay open for the lifetime of the workspace.
    private static let eventSession = URLSession(configuration: .ephemeral)

    private let port: Int
    private let session: URLSession

    public init(port: Int = 18900, session: URLSession? = nil) {
        self.port = port
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 300
        self.session = session ?? URLSession(configuration: configuration)
    }

    public func health(timeout: TimeInterval? = nil) async throws -> Bool {
        struct Health: Decodable { let status: String }
        let result: Health = try await request(["health"], timeout: timeout)
        return result.status == "ok"
    }

    public func projects() async throws -> [Project] {
        try await request(["api", "projects"])
    }

    public func reorderProjects(_ ids: [String]) async throws -> [Project] {
        struct Order: Encodable { let projectIds: [String] }
        return try await write(["api", "projects", "order"], method: "PUT", body: Order(projectIds: ids))
    }

    public func minutes(projectId: String? = nil, limit: Int = 40, offset: Int = 0) async throws -> [Minutes] {
        var query = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "offset", value: String(offset)),
        ]
        if let projectId { query.append(URLQueryItem(name: "project", value: projectId)) }
        return try await request(["api", "minutes"], query: query)
    }

    public func minutes(id: String, includeTranscript: Bool = true) async throws -> Minutes {
        let query = includeTranscript ? [] : [URLQueryItem(name: "include_transcript", value: "false")]
        return try await request(["api", "minutes", id], query: query)
    }

    public func transcript(id: String) async throws -> [TranscriptSegment] {
        struct Payload: Decodable { let transcript: [TranscriptSegment] }
        let payload: Payload = try await request(["api", "minutes", id, "transcript"])
        return payload.transcript
    }

    public func minutesCount(projectId: String) async throws -> Int {
        struct MinutesCount: Decodable { let count: Int }
        let result: MinutesCount = try await request(["api", "projects", projectId, "minutes", "count"])
        return result.count
    }

    public func search(_ text: String, projectId: String? = nil) async throws -> [Minutes] {
        var query = [URLQueryItem(name: "q", value: text)]
        if let projectId { query.append(URLQueryItem(name: "project", value: projectId)) }
        return try await request(["api", "minutes", "search"], query: query)
    }

    public func devices(refresh: Bool = false) async throws -> AudioDevices {
        try await request(["api", "devices"], query: refresh ? [.init(name: "refresh", value: "true")] : [])
    }

    public func start(projectId: String, micDevice: Int?, captureSystem: Bool) async throws -> RecordingStart {
        struct Start: Encodable {
            let projectId: String
            let micDevice: Int?
            let captureSystem: Bool
        }
        return try await write(["api", "recording", "start"], method: "POST",
                               body: Start(projectId: projectId, micDevice: micDevice, captureSystem: captureSystem))
    }

    public func stop() async throws -> RecordingEnd {
        try await request(["api", "recording", "stop"], method: "POST", timeout: 8)
    }

    public func recordingStatus(timeout: TimeInterval? = nil) async throws -> RecordingStatus {
        try await request(["api", "recording", "status"], timeout: timeout)
    }

    public func setPaused(_ paused: Bool) async throws -> (paused: Bool, elapsed: Double?) {
        struct Request: Encodable { let paused: Bool }
        struct Response: Decodable { let paused: Bool; let elapsedSec: Double? }
        let result: Response = try await write(["api", "recording", "pause"], method: "POST", body: Request(paused: paused))
        return (result.paused, result.elapsedSec)
    }

    public func setMicMuted(_ muted: Bool) async throws -> Bool {
        struct Mute: Codable { let muted: Bool }
        let result: Mute = try await write(["api", "recording", "mic-mute"], method: "POST", body: Mute(muted: muted))
        return result.muted
    }

    public func recordingMicrophones() async throws -> [RecordingMicrophone] {
        struct Microphones: Decodable { let devices: [RecordingMicrophone] }
        let result: Microphones = try await request(["api", "recording", "microphones"])
        return result.devices
    }

    public func switchMicrophone(sessionId: String, deviceUid: String?) async throws -> MicrophoneChange {
        struct Selection: Encodable {
            let sessionId: String
            let deviceUid: String?
        }
        return try await write(["api", "recording", "microphone"], method: "PUT",
                               body: Selection(sessionId: sessionId, deviceUid: deviceUid))
    }

    public func segments(in sessionId: String) async throws -> [TranscriptSegment] {
        try await request(["api", "recording", "sessions", sessionId, "segments"])
    }

    public func pipelines() async throws -> [PipelineStatus] {
        try await request(["api", "recording", "pipelines"])
    }

    public func recoveryStatus() async throws -> RecoveryStatus {
        try await request(["api", "recording", "recovery", "status"])
    }

    public func audioURL(for sessionId: String) throws -> URL {
        try endpoint(["api", "recording", "play", sessionId], query: [.init(name: "v", value: "flac-playback-2")])
    }

    public func events() throws -> BackendEventConnection {
        let socket = Self.eventSession.webSocketTask(with: try endpoint(["ws"], scheme: "ws"))
        socket.resume()
        return BackendEventConnection(socket: socket)
    }

    func request<Value: Decodable>(
        _ path: [String], query: [URLQueryItem] = [], method: String = "GET", body: Data? = nil,
        timeout: TimeInterval? = nil
    ) async throws -> Value {
        var request = URLRequest(url: try endpoint(path, query: query))
        request.httpMethod = method
        request.httpBody = body
        if let timeout { request.timeoutInterval = timeout }
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        guard (200...299).contains(http.statusCode) else {
            let payload = try? JSONDecoder().decode(ErrorPayload.self, from: data)
            let detail = payload?.error ?? payload?.detail
            throw BackendError(status: http.statusCode, code: detail?.code,
                               message: detail?.message ?? "処理に失敗しました（HTTP \(http.statusCode)）")
        }
        return try makeDecoder().decode(Value.self, from: data)
    }

    func write<Value: Decodable, Body: Encodable>(
        _ path: [String], method: String, body: Body, timeout: TimeInterval? = nil
    ) async throws -> Value {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return try await request(path, method: method, body: encoder.encode(body), timeout: timeout)
    }

    func endpoint(
        _ path: [String], query: [URLQueryItem] = [], scheme: String = "http"
    ) throws -> URL {
        var components = URLComponents()
        components.scheme = scheme
        components.host = "127.0.0.1"
        components.port = port
        let segmentCharacters = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-._~"))
        components.percentEncodedPath = try "/" + path.map {
            guard !$0.isEmpty, $0 != ".", $0 != "..",
                  let encoded = $0.addingPercentEncoding(withAllowedCharacters: segmentCharacters) else {
                throw URLError(.badURL)
            }
            return encoded
        }.joined(separator: "/")
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw URLError(.badURL) }
        return url
    }

    private struct ErrorPayload: Decodable {
        struct Detail: Decodable {
            let code: String?
            let message: String?

            private enum Keys: CodingKey { case code, message }

            init(from decoder: Decoder) throws {
                if let text = try? decoder.singleValueContainer().decode(String.self) {
                    code = nil
                    message = text
                } else {
                    let contents = try decoder.container(keyedBy: Keys.self)
                    code = try contents.decodeIfPresent(String.self, forKey: .code)
                    message = try contents.decodeIfPresent(String.self, forKey: .message)
                }
            }
        }
        let error: Detail?
        let detail: Detail?
    }
}

public struct BackendEventConnection: Sendable {
    fileprivate let socket: URLSessionWebSocketTask

    public func next() async throws -> BackendEvent {
        try await withTaskCancellationHandler {
            let message = try await socket.receive()
            switch message {
            case .data(let data): return try makeDecoder().decode(BackendEvent.self, from: data)
            case .string(let text): return try makeDecoder().decode(BackendEvent.self, from: Data(text.utf8))
            @unknown default: throw URLError(.cannotDecodeContentData)
            }
        } onCancel: {
            close()
        }
    }

    public func close() {
        socket.cancel(with: .normalClosure, reason: nil)
    }
}

private func makeDecoder() -> JSONDecoder {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return decoder
}
