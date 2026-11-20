@preconcurrency import AVFoundation
import Foundation

public struct SpeechDraftUpdate: Codable, Equatable, Sendable {
    struct Part: Codable, Equatable, Sendable {
        let start: Double
        let text: String
    }

    public let start: Double
    public let end: Double
    public let text: String
    public let isFinal: Bool
    let parts: [Part]
}

#if compiler(>=6.2) && canImport(Speech)
import Speech

public enum AppleSpeechDraftError: LocalizedError {
    case unsupportedLanguage
    case missingModel
    case unsupportedAudio
    case sessionAlreadyUsed
    case inputBacklog

    public var errorDescription: String? {
        switch self {
        case .unsupportedLanguage: "この言語のオンデバイス認識は利用できません"
        case .missingModel: "オンデバイス認識の音声モデルが必要です"
        case .unsupportedAudio: "音声形式を認識用に変換できません"
        case .sessionAlreadyUsed: "この音声認識セッションは終了しています"
        case .inputBacklog: "音声速報の処理が追いつきませんでした"
        }
    }
}

/// Uses the local speech model for low latency preview text. The caller
/// supplies the same audio frames used for recording and owns durable storage.
@available(macOS 26.0, *)
public actor AppleSpeechDraft {
    public nonisolated let inputFormat: AVAudioFormat
    private let transcriber: SpeechTranscriber
    private let analyzer: SpeechAnalyzer
    private var used = false

    public init(
        locale: Locale, sourceFormat: AVAudioFormat? = nil, installModelIfNeeded: Bool = false
    ) async throws {
        guard SpeechTranscriber.isAvailable,
              let locale = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            throw AppleSpeechDraftError.unsupportedLanguage
        }
        let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [],
                                            reportingOptions: [.volatileResults, .fastResults],
                                            attributeOptions: [.audioTimeRange])
        let modules: [any SpeechModule] = [transcriber]
        if await AssetInventory.status(forModules: modules) != .installed {
            guard installModelIfNeeded else { throw AppleSpeechDraftError.missingModel }
            if let request = try await AssetInventory.assetInstallationRequest(supporting: modules) {
                try await request.downloadAndInstall()
            }
        }
        guard await AssetInventory.status(forModules: modules) == .installed else {
            throw AppleSpeechDraftError.missingModel
        }
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: modules,
                                                                         considering: sourceFormat) else {
            throw AppleSpeechDraftError.unsupportedAudio
        }
        let analyzer = SpeechAnalyzer(modules: modules)
        try await analyzer.prepareToAnalyze(in: format)
        self.analyzer = analyzer
        self.transcriber = transcriber
        inputFormat = format
    }

    public typealias SubmitAudio = @Sendable (AnalyzerInput) throws -> Void

    public func analyze(
        onUpdate: @escaping @Sendable (SpeechDraftUpdate) async -> Void,
        feed: @escaping @Sendable (AVAudioFormat, SubmitAudio) async throws -> Void
    ) async throws {
        guard !used else { throw AppleSpeechDraftError.sessionAlreadyUsed }
        used = true
        let (input, continuation) = AsyncStream<AnalyzerInput>.makeStream(bufferingPolicy: .bufferingOldest(48))
        async let readings: Void = relayResults(onUpdate)
        do {
            try await analyzer.start(inputSequence: input)
            try await feed(inputFormat) { frame in
                switch continuation.yield(frame) {
                case .enqueued: return
                case .dropped: throw AppleSpeechDraftError.inputBacklog
                case .terminated: throw CancellationError()
                @unknown default: throw AppleSpeechDraftError.inputBacklog
                }
            }
            continuation.finish()
            try await analyzer.finalizeAndFinishThroughEndOfInput()
            try await readings
        } catch {
            continuation.finish()
            await analyzer.cancelAndFinishNow()
            throw error
        }
    }

    private func relayResults(
        _ onUpdate: @Sendable (SpeechDraftUpdate) async -> Void
    ) async throws {
        for try await result in transcriber.results {
            let attributed = result.text
            let text = String(attributed.characters)
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            let start = result.range.start.seconds
            guard start.isFinite else { continue }
            let end = result.range.end.seconds
            var partStart = start
            let parts = attributed.runs.map { run in
                if let time = run.audioTimeRange?.start.seconds, time.isFinite { partStart = time }
                return SpeechDraftUpdate.Part(start: partStart, text: String(attributed[run.range].characters))
            }
            await onUpdate(.init(start: start, end: end.isFinite ? end : start, text: text,
                                 isFinal: result.isFinal, parts: parts))
        }
    }
}
#endif
