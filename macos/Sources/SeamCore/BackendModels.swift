import Foundation

public struct Project: Decodable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let outputDir: String
    public let repoPath: String?
    public let docDirs: [String]?
    public let glossary: [String]?
    public let corrections: [ProjectCorrection]?
}

public struct ProjectCorrection: Codable, Equatable, Sendable {
    public let wrong: String
    public let correct: String

    public init(wrong: String, correct: String) {
        self.wrong = wrong
        self.correct = correct
    }
}

public struct Minutes: Decodable, Identifiable, Sendable {
    public struct Highlights: Decodable, Sendable {
        public let title: String?
        public let summary: String?
        public let transcript: String?
    }

    public let id: String
    public let sessionId: String
    public let projectId: String
    public let title: String
    public let date: String
    public let startedAt: String
    public let durationSec: Double
    public let summary: String?
    public let transcript: [TranscriptSegment]?
    public let highlights: Highlights?
}

public struct AudioDevice: Decodable, Identifiable, Sendable {
    public let id: Int
    public let name: String
    public let isDefault: Bool
    public let channels: Int
}

public struct AudioDevices: Decodable, Sendable {
    public let devices: [AudioDevice]
    public let systemAudioAvailable: Bool?
    public let screenCaptureAvailable: Bool
}

public struct RecordingStatus: Decodable, Sendable {
    public let recording: Bool
    public let elapsedSec: Double
    public let sessionId: String?
    public let micMuted: Bool?
    public let paused: Bool?
    public let stopping: Bool?
    public let transcriptionDeferred: Bool?
}

public struct RecordingStart: Decodable, Sendable {
    public let sessionId: String
    public let hasSystemAudio: Bool
    public let transcriptionDeferred: Bool?
}

public struct RecordingEnd: Decodable, Sendable {
    public let status: String
    public let sessionId: String?
}

public struct RecordingResult: Decodable, Sendable {
    public let sessionId: String?
    public let durationSec: Double?
    public let wavPath: String?
    public let error: String?
}

public struct RecordingUpdate: Decodable, Sendable {
    public let sessionId: String
    public let elapsedSec: Double
    public let paused: Bool?
    public let transcriptionDeferred: Bool?
}

public struct AudioLevel: Decodable, Sendable {
    public let sessionId: String
    public let mic: Double
    public let level: Double?

    public var combinedLevel: Double { min(1, max(0, level ?? mic)) }
}

public struct TranscriptSegment: Decodable, Equatable, Identifiable, Sendable {
    public let start: Double
    public let end: Double
    public let text: String
    public let speakerId: String?
    public let speakerLabel: String?
    public let sessionId: String?

    public var id: Double { start }

    public init(start: Double, end: Double, text: String, speakerId: String? = nil,
                speakerLabel: String? = nil, sessionId: String? = nil) {
        self.start = start
        self.end = end
        self.text = text
        self.speakerId = speakerId
        self.speakerLabel = speakerLabel
        self.sessionId = sessionId
    }
}

public struct TranscriptDraft: Decodable, Equatable, Sendable {
    public let sessionId: String
    public let start: Double
    public let end: Double?
    public let text: String
    public let segments: [TranscriptSegment]?
}

public struct PipelineStatus: Decodable, Sendable {
    public let sessionId: String?
    public let projectId: String?
    public let state: String
    public let message: String
    public let progress: Double?
    public let stageLabel: String?
    public let error: String?
}

public struct RecoveryStatus: Decodable, Sendable {
    public struct Item: Decodable, Sendable {
        public let sessionId: String
        public let date: String
        public let hhmm: String
    }

    public let state: String
    public let total: Int
    public let current: Int
    public let item: Item?
    public let recovered: Int
}

public struct RecordingIdleReminder: Decodable, Sendable {
    public let sessionId: String
    public let silentForSec: Int
}

public enum BackendEvent: Decodable, Sendable {
    case recording(RecordingUpdate)
    case stopped(RecordingResult)
    case recordingIdle(RecordingIdleReminder)
    case level(AudioLevel)
    case draft(TranscriptDraft)
    case segment(TranscriptSegment)
    case pipeline(PipelineStatus)
    case other(String)

    private enum Keys: CodingKey { case type, data }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: Keys.self)
        let type = try container.decode(String.self, forKey: .type)
        switch type {
        case "recording_status":
            self = .recording(try container.decode(RecordingUpdate.self, forKey: .data))
        case "recording_stopped":
            self = .stopped(try container.decode(RecordingResult.self, forKey: .data))
        case "recording_idle_reminder":
            self = .recordingIdle(try container.decode(RecordingIdleReminder.self, forKey: .data))
        case "audio_level":
            self = .level(try container.decode(AudioLevel.self, forKey: .data))
        case "transcript_preview":
            self = .draft(try container.decode(TranscriptDraft.self, forKey: .data))
        case "transcript_chunk":
            self = .segment(try container.decode(TranscriptSegment.self, forKey: .data))
        case "pipeline_progress":
            self = .pipeline(try container.decode(PipelineStatus.self, forKey: .data))
        default:
            self = .other(type)
        }
    }
}
