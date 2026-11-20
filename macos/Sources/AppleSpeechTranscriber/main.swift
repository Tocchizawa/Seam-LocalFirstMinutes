@preconcurrency import AVFoundation
import Darwin
import Foundation
import SeamCore

#if compiler(>=6.2) && canImport(Speech)
import Speech

private final class EventWriter: @unchecked Sendable {
    private let lock = NSLock()
    private let sessionID: String?

    init(sessionID: String?) { self.sessionID = sessionID }

    func update(_ update: SpeechDraftUpdate) async {
        if update.isFinal {
            write(type: "transcript_chunk", data: [
                "start": update.start,
                "end": update.end,
                "text": update.text,
                "session_id": sessionID as Any? ?? NSNull(),
            ])
            write(type: "transcript_preview", data: [
                "start": update.start,
                "end": update.end,
                "text": "",
                "session_id": sessionID as Any? ?? NSNull(),
            ])
        } else {
            write(type: "transcript_preview", data: [
                "start": update.start,
                "end": update.end,
                "text": update.text,
                "session_id": sessionID as Any? ?? NSNull(),
            ])
        }
    }

    func error(_ message: String) {
        write(type: "transcription_error", data: ["message": message])
    }

    private func write(type: String, data: [String: Any]) {
        do {
            let payload = try JSONSerialization.data(withJSONObject: ["type": type, "data": data])
            lock.lock()
            defer { lock.unlock() }
            FileHandle.standardOutput.write(payload)
            FileHandle.standardOutput.write(Data([0x0a]))
        } catch {
            FileHandle.standardError.write(Data("Apple Speech event encoding failed: \(error)\n".utf8))
        }
    }
}

private struct Options {
    let wavPath: String
    let language: String
    let followsFile: Bool
    let stopFile: String?
    let sessionID: String?

    init(arguments: [String]) throws {
        var wavPath: String?
        var language = "ja"
        var followsFile = false
        var stopFile: String?
        var sessionID: String?
        var index = 1
        while index < arguments.count {
            let key = arguments[index]
            index += 1
            switch key {
            case "--follow": followsFile = true
            case "--wav", "--language", "--stop-file", "--session-id":
                guard index < arguments.count else { throw CliError.usage }
                let value = arguments[index]
                index += 1
                switch key {
                case "--wav": wavPath = value
                case "--language": language = value
                case "--stop-file": stopFile = value
                case "--session-id": sessionID = value
                default: break
                }
            default: throw CliError.usage
            }
        }
        guard let wavPath else { throw CliError.usage }
        self.wavPath = wavPath
        self.language = language
        self.followsFile = followsFile
        self.stopFile = stopFile
        self.sessionID = sessionID
    }
}

private enum CliError: LocalizedError {
    case usage
    var errorDescription: String? {
        "Usage: AppleSpeechTranscriber --wav <pcm-wav> [--follow --stop-file <path>] [--language <locale>] [--session-id <id>]"
    }
}

@main
private struct AppleSpeechTranscriberCommand {
    static func main() async {
        do {
            let options = try Options(arguments: CommandLine.arguments)
            let writer = EventWriter(sessionID: options.sessionID)
            guard #available(macOS 26.0, *) else {
                writer.error("Apple Speech のオンデバイス認識には macOS 26 以降が必要です")
                exit(2)
            }
            try await run(options, writer: writer)
        } catch {
            EventWriter(sessionID: nil).error(error.localizedDescription)
            exit(1)
        }
    }

    @available(macOS 26.0, *)
    private static func run(_ options: Options, writer: EventWriter) async throws {
        let source = try LiveWaveReader(path: options.wavPath)
        let draft = try await AppleSpeechDraft(
            locale: Locale(identifier: options.language == "auto"
                            ? (Locale.preferredLanguages.first ?? "en")
                            : options.language),
            sourceFormat: source.format,
            installModelIfNeeded: true
        )
        try await draft.analyze(onUpdate: { update in
            await writer.update(update)
        }) { target, submit in
            while true {
                if let input = try source.read(target: target) {
                    try submit(input)
                    continue
                }
                let shouldFinish = !options.followsFile || options.stopFile.map {
                    FileManager.default.fileExists(atPath: $0)
                } == true
                if shouldFinish { break }
                try await Task.sleep(for: .milliseconds(60))
            }
        }
    }
}

@available(macOS 26.0, *)
private final class LiveWaveReader {
    private static let headerBytes: UInt64 = 44
    private static let bytesPerFrame: UInt64 = 2
    private let handle: FileHandle
    let format: AVAudioFormat
    private let converterCache = NSCache<NSString, AVAudioConverterBox>()
    private var sentFrames: AVAudioFramePosition = 0

    init(path: String) throws {
        handle = try FileHandle(forReadingFrom: URL(fileURLWithPath: path))
        guard let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000,
                                         channels: 1, interleaved: false) else {
            throw AppleSpeechDraftError.unsupportedAudio
        }
        self.format = format
        try handle.seek(toOffset: Self.headerBytes)
    }

    deinit { try? handle.close() }

    func read(target: AVAudioFormat) throws -> AnalyzerInput? {
        let byteCount = Int(format.sampleRate / 10) * Int(Self.bytesPerFrame)
        guard let bytes = try handle.read(upToCount: byteCount), !bytes.isEmpty else { return nil }
        let frames = bytes.count / Int(Self.bytesPerFrame)
        let completeBytes = frames * Int(Self.bytesPerFrame)
        if completeBytes != bytes.count { try handle.seek(toOffset: handle.offset() - 1) }
        guard frames > 0,
              let input = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)),
              let channel = input.int16ChannelData?.pointee else {
            throw AppleSpeechDraftError.unsupportedAudio
        }
        _ = bytes.copyBytes(to: UnsafeMutableRawBufferPointer(start: channel, count: completeBytes),
                            from: 0..<completeBytes)
        input.frameLength = AVAudioFrameCount(frames)
        let buffer = try convert(input, target: target)
        guard buffer.frameLength > 0 else { return nil }
        let offset = CMTime(value: CMTimeValue(sentFrames), timescale: CMTimeScale(target.sampleRate))
        sentFrames += AVAudioFramePosition(buffer.frameLength)
        return AnalyzerInput(buffer: buffer, bufferStartTime: offset)
    }

    private func convert(_ input: AVAudioPCMBuffer, target: AVAudioFormat) throws -> AVAudioPCMBuffer {
        guard format != target else { return input }
        let key = "\(target.sampleRate)-\(target.channelCount)-\(target.commonFormat.rawValue)" as NSString
        let converter: AVAudioConverter
        if let cached = converterCache.object(forKey: key)?.value {
            converter = cached
        } else {
            guard let created = AVAudioConverter(from: format, to: target) else {
                throw AppleSpeechDraftError.unsupportedAudio
            }
            converterCache.setObject(AVAudioConverterBox(created), forKey: key)
            converter = created
        }
        let capacity = ceil(Double(input.frameLength) * target.sampleRate / format.sampleRate) + 32
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: AVAudioFrameCount(capacity)) else {
            throw AppleSpeechDraftError.unsupportedAudio
        }
        var pending: AVAudioPCMBuffer? = input
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, status in
            let buffer = pending
            pending = nil
            status.pointee = buffer == nil ? .noDataNow : .haveData
            return buffer
        }
        guard status != .error else { throw error ?? AppleSpeechDraftError.unsupportedAudio }
        return output
    }
}

@available(macOS 26.0, *)
private final class AVAudioConverterBox: NSObject {
    let value: AVAudioConverter
    init(_ value: AVAudioConverter) { self.value = value }
}
#else
@main
private struct AppleSpeechTranscriberCommand {
    static func main() {
        FileHandle.standardError.write(Data("This build does not include Apple Speech support.\n".utf8))
        Foundation.exit(2)
    }
}
#endif
