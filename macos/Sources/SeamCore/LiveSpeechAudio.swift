@preconcurrency import AVFoundation
import Foundation

#if compiler(>=6.2) && canImport(Speech)
import Speech

@available(macOS 26.0, *)
extension LiveAudioSource {
    public func transcribe(onUpdate: @escaping @Sendable (SpeechDraftUpdate) async -> Void) async throws {
        let language = language == "auto" ? Locale.preferredLanguages.first ?? "en" : language
        guard sampleRate > 0,
              let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(sampleRate),
                                         channels: 1, interleaved: false) else {
            throw AppleSpeechDraftError.unsupportedAudio
        }
        let draft = try await AppleSpeechDraft(locale: Locale(identifier: language), sourceFormat: format,
                                               installModelIfNeeded: true)
        try Task.checkCancellation()
        try await draft.analyze(onUpdate: onUpdate) { target, submit in
            let reader = try LiveWaveReader(path: path, source: format, target: target)
            while !Task.isCancelled {
                if let input = try reader.read() {
                    try submit(input)
                } else {
                    try await Task.sleep(for: .milliseconds(60))
                }
            }
            try Task.checkCancellation()
        }
    }
}

@available(macOS 26.0, *)
private final class LiveWaveReader {
    // RealtimeMixedAudioWriter writes a standard 44-byte PCM WAV header.
    private static let headerBytes: UInt64 = 44
    private static let bytesPerFrame: UInt64 = 2
    private let handle: FileHandle
    private let source: AVAudioFormat
    private let target: AVAudioFormat
    private let converter: AVAudioConverter?
    private let firstFrame: UInt64
    private var sentFrames: AVAudioFramePosition = 0

    init(path: String, source: AVAudioFormat, target: AVAudioFormat) throws {
        handle = try FileHandle(forReadingFrom: URL(fileURLWithPath: path))
        self.source = source
        self.target = target
        converter = source == target ? nil : AVAudioConverter(from: source, to: target)
        guard source == target || converter != nil else { throw AppleSpeechDraftError.unsupportedAudio }
        let end = try handle.seekToEnd()
        let frames = end > Self.headerBytes ? (end - Self.headerBytes) / Self.bytesPerFrame : 0
        // A reopened workspace can join the live stream while Whisper restores older text.
        let leadIn = UInt64(source.sampleRate)
        firstFrame = frames > leadIn ? frames - leadIn : 0
        try handle.seek(toOffset: Self.headerBytes + firstFrame * Self.bytesPerFrame)
    }

    deinit { try? handle.close() }

    func read() throws -> AnalyzerInput? {
        let byteCount = Int(source.sampleRate / 10) * Int(Self.bytesPerFrame)
        guard let bytes = try handle.read(upToCount: byteCount), !bytes.isEmpty else { return nil }
        let frames = bytes.count / Int(Self.bytesPerFrame)
        let completeBytes = frames * Int(Self.bytesPerFrame)
        if completeBytes != bytes.count { try handle.seek(toOffset: handle.offset() - 1) }
        guard frames > 0 else { return nil }
        guard let input = AVAudioPCMBuffer(pcmFormat: source, frameCapacity: AVAudioFrameCount(frames)),
              let channel = input.int16ChannelData?.pointee else {
            throw AppleSpeechDraftError.unsupportedAudio
        }
        _ = bytes.copyBytes(to: UnsafeMutableRawBufferPointer(start: channel, count: completeBytes),
                            from: 0..<completeBytes)
        input.frameLength = AVAudioFrameCount(frames)
        let buffer = try convert(input)
        guard buffer.frameLength > 0 else { return nil }
        let start = CMTime(value: CMTimeValue(firstFrame), timescale: CMTimeScale(source.sampleRate))
        let offset = CMTime(value: sentFrames, timescale: CMTimeScale(target.sampleRate))
        sentFrames += AVAudioFramePosition(buffer.frameLength)
        return AnalyzerInput(buffer: buffer, bufferStartTime: start + offset)
    }

    private func convert(_ input: AVAudioPCMBuffer) throws -> AVAudioPCMBuffer {
        guard let converter else { return input }
        let frames = ceil(Double(input.frameLength) * target.sampleRate / source.sampleRate)
        guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: AVAudioFrameCount(frames) + 32) else {
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
#endif
