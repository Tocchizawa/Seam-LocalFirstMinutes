import Foundation
import ScreenCaptureKit
import CoreMedia
import AVFoundation

/// ScreenCaptureKit でシステム音声をキャプチャし、
/// RAW PCM (16kHz, mono, int16LE) をファイルに書き出す CLI ツール。
///
/// Usage: audio-capture <output-path> [--sample-rate 16000]
/// 停止: SIGINT (Ctrl+C) or SIGTERM

// MARK: - Args

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("Usage: audio-capture <output.raw> [--sample-rate 16000]\n", stderr)
    exit(1)
}

let outputPath = args[1]
var targetSampleRate: Double = 16000

if let srIdx = args.firstIndex(of: "--sample-rate"), srIdx + 1 < args.count,
   let sr = Double(args[srIdx + 1]) {
    targetSampleRate = sr
}

// MARK: - Audio Capture Delegate

class AudioOutputHandler: NSObject, SCStreamOutput {
    let fileHandle: FileHandle
    let targetRate: Double
    var converter: AVAudioConverter?
    var inputFormat: AVAudioFormat?

    init(fileHandle: FileHandle, targetRate: Double) {
        self.fileHandle = fileHandle
        self.targetRate = targetRate
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard sampleBuffer.isValid, sampleBuffer.numSamples > 0 else { return }

        guard let formatDesc = sampleBuffer.formatDescription,
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc)?.pointee else {
            return
        }

        // Setup converter on first buffer
        if converter == nil {
            guard let srcFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: asbd.mSampleRate,
                channels: AVAudioChannelCount(asbd.mChannelsPerFrame),
                interleaved: false
            ) else { return }

            guard let dstFormat = AVAudioFormat(
                commonFormat: .pcmFormatInt16,
                sampleRate: targetRate,
                channels: 1,
                interleaved: true
            ) else { return }

            inputFormat = srcFormat
            converter = AVAudioConverter(from: srcFormat, to: dstFormat)
            if converter == nil {
                fputs("Failed to create audio converter\n", stderr)
            }
            fputs("Audio capture started: \(Int(asbd.mSampleRate))Hz \(asbd.mChannelsPerFrame)ch → \(Int(targetRate))Hz mono int16\n", stderr)
        }

        guard let converter = converter, let inputFormat = inputFormat else { return }

        // Extract PCM buffer from CMSampleBuffer
        guard let blockBuffer = sampleBuffer.dataBuffer else { return }
        let length = CMBlockBufferGetDataLength(blockBuffer)
        var dataPointer: UnsafeMutablePointer<Int8>?
        CMBlockBufferGetDataPointer(blockBuffer, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: nil, dataPointerOut: &dataPointer)
        guard let ptr = dataPointer else { return }

        let frameCount = AVAudioFrameCount(sampleBuffer.numSamples)

        guard let inputBuffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: frameCount) else { return }
        inputBuffer.frameLength = frameCount

        // Copy data into the input buffer
        let channelCount = Int(inputFormat.channelCount)
        let bytesPerFrame = Int(asbd.mBytesPerFrame)

        if inputFormat.isInterleaved || channelCount == 1 {
            memcpy(inputBuffer.floatChannelData![0], ptr, min(length, Int(frameCount) * MemoryLayout<Float>.size * channelCount))
        } else {
            // Non-interleaved: SCK provides interleaved float32, split into channels
            let floatPtr = UnsafeRawPointer(ptr).bindMemory(to: Float.self, capacity: Int(frameCount) * channelCount)
            for ch in 0..<channelCount {
                let dst = inputBuffer.floatChannelData![ch]
                for i in 0..<Int(frameCount) {
                    dst[i] = floatPtr[i * channelCount + ch]
                }
            }
        }

        // Convert
        let outputFrameCapacity = AVAudioFrameCount(Double(frameCount) * targetRate / inputFormat.sampleRate) + 16
        guard let outputBuffer = AVAudioPCMBuffer(
            pcmFormat: converter.outputFormat,
            frameCapacity: outputFrameCapacity
        ) else { return }

        var error: NSError?
        var isDone = false
        converter.convert(to: outputBuffer, error: &error) { _, outStatus in
            if isDone {
                outStatus.pointee = .noDataNow
                return nil
            }
            isDone = true
            outStatus.pointee = .haveData
            return inputBuffer
        }

        if let error = error {
            fputs("Conversion error: \(error)\n", stderr)
            return
        }

        // Write raw int16 PCM
        if let int16Data = outputBuffer.int16ChannelData {
            let byteCount = Int(outputBuffer.frameLength) * MemoryLayout<Int16>.size
            let data = Data(bytes: int16Data[0], count: byteCount)
            fileHandle.write(data)
        }
    }
}

// MARK: - Main

func run() async {
    // Get shareable content
    let content: SCShareableContent
    do {
        content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    } catch {
        fputs("Failed to get shareable content (permission denied?): \(error)\n", stderr)
        fputs("Grant Screen Recording permission in System Settings > Privacy & Security\n", stderr)
        exit(1)
    }

    guard let display = content.displays.first else {
        fputs("No display found\n", stderr)
        exit(1)
    }

    // Configure: audio only (minimal video)
    let config = SCStreamConfiguration()
    config.capturesAudio = true
    config.excludesCurrentProcessAudio = true
    config.width = 2
    config.height = 2
    config.minimumFrameInterval = CMTime(value: 1, timescale: 1) // 1fps min video
    config.sampleRate = 48000 // SCK default
    config.channelCount = 2

    // Create stream (capture entire display)
    let filter = SCContentFilter(display: display, excludingWindows: [])
    let stream = SCStream(filter: filter, configuration: config, delegate: nil)

    // Open output file
    FileManager.default.createFile(atPath: outputPath, contents: nil)
    guard let fileHandle = FileHandle(forWritingAtPath: outputPath) else {
        fputs("Failed to open output file: \(outputPath)\n", stderr)
        exit(1)
    }

    let handler = AudioOutputHandler(fileHandle: fileHandle, targetRate: targetSampleRate)

    do {
        try stream.addStreamOutput(handler, type: .audio, sampleHandlerQueue: DispatchQueue(label: "audio-capture"))
        try await stream.startCapture()
    } catch {
        fputs("Failed to start capture: \(error)\n", stderr)
        exit(1)
    }

    fputs("Capturing system audio → \(outputPath) (16kHz mono int16LE)\n", stderr)
    fputs("Press Ctrl+C to stop\n", stderr)

    // Handle SIGINT/SIGTERM
    let sigSrc = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
    signal(SIGINT, SIG_IGN)
    sigSrc.setEventHandler {
        fputs("\nStopping capture...\n", stderr)
        Task {
            try? await stream.stopCapture()
            fileHandle.closeFile()
            let size = (try? FileManager.default.attributesOfItem(atPath: outputPath)[.size] as? Int) ?? 0
            let durationSec = Double(size) / (targetSampleRate * 2) // int16 = 2 bytes
            fputs("Saved: \(outputPath) (\(String(format: "%.1f", durationSec))s)\n", stderr)
            exit(0)
        }
    }
    sigSrc.resume()

    let termSrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    signal(SIGTERM, SIG_IGN)
    termSrc.setEventHandler {
        Task {
            try? await stream.stopCapture()
            fileHandle.closeFile()
            exit(0)
        }
    }
    termSrc.resume()

    // Keep running
    RunLoop.main.run()
}

// Entry point
if #available(macOS 13.0, *) {
    Task { await run() }
    dispatchMain()
} else {
    fputs("macOS 13.0+ required\n", stderr)
    exit(1)
}
