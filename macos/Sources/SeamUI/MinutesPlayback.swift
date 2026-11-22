@preconcurrency import AVFoundation
import SwiftUI

@MainActor
public final class MinutesPlayback: ObservableObject {
    @Published public private(set) var elapsed = 0.0
    @Published public private(set) var duration = 0.0
    @Published public private(set) var isPlaying = false
    @Published public private(set) var rate = 1.0
    @Published public private(set) var hasPosition = false
    @Published public private(set) var error: String?

    private let player = AVPlayer()
    private var clock: Any?
    private var atEnd: NSObjectProtocol?
    private var loadTask: Task<Void, Never>?
    private var itemObservation: NSKeyValueObservation?
    private var sourceID: String?
    private var seekRevision = 0

    public init(muted: Bool = false) {
        player.isMuted = muted
        clock = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.10, preferredTimescale: 600),
                                               queue: .main) { [weak self] time in
            Task { @MainActor in
                guard let self, time.seconds.isFinite else { return }
                // A queued callback from before a seek may contain an older time.
                guard abs(self.player.currentTime().seconds - time.seconds) < 0.3 else { return }
                self.elapsed = max(0, time.seconds)
            }
        }
    }

    deinit {
        if let clock { player.removeTimeObserver(clock) }
        if let atEnd { NotificationCenter.default.removeObserver(atEnd) }
        loadTask?.cancel()
    }

    public func load(id: String, url: URL, durationHint: Double? = nil) {
        guard sourceID != id else { return }
        clear()
        sourceID = id
        let bound = durationHint.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
        duration = bound ?? 0
        let item = AVPlayerItem(url: url)
        if let bound {
            item.forwardPlaybackEndTime = CMTime(seconds: bound, preferredTimescale: 600)
        }
        player.replaceCurrentItem(with: item)
        itemObservation = item.observe(\.status, options: [.initial, .new]) { [weak self] item, _ in
            let failure = item.status == .failed ? item.error?.localizedDescription ?? "音声を読み込めません" : nil
            guard let failure else { return }
            Task { @MainActor in
                guard let self, self.sourceID == id else { return }
                self.pause()
                self.error = failure
            }
        }
        atEnd = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main) {
            [weak self] _ in
            Task { @MainActor in
                guard let self, self.sourceID == id else { return }
                self.isPlaying = false
                self.elapsed = self.duration
            }
        }
        loadTask = Task { [weak self] in
            guard let loaded = try? await item.asset.load(.duration), loaded.seconds.isFinite,
                  let self, self.sourceID == id, !Task.isCancelled else { return }
            self.duration = bound.map { min($0, loaded.seconds) } ?? loaded.seconds
        }
    }

    public func toggle() {
        guard player.currentItem != nil, error == nil else { return }
        if isPlaying {
            pause()
        } else if duration > 0 && elapsed >= duration - 0.06 {
            seek(to: 0, play: true)
        } else {
            hasPosition = true
            isPlaying = true
            player.playImmediately(atRate: Float(rate))
        }
    }

    public func seek(to time: Double, play: Bool = false) {
        guard player.currentItem != nil, time.isFinite else { return }
        let target = max(0, time)
        let position = duration > 0 ? min(duration, target) : target
        seekRevision += 1
        let revision = seekRevision
        hasPosition = true
        elapsed = position
        if play { isPlaying = true }
        player.seek(to: CMTime(seconds: position, preferredTimescale: 600),
                    toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] finished in
            Task { @MainActor in
                guard let self, self.seekRevision == revision, finished else { return }
                if self.isPlaying { self.player.playImmediately(atRate: Float(self.rate)) }
            }
        }
    }

    public func setRate(_ rate: Double) {
        guard (0.5...2).contains(rate) else { return }
        self.rate = rate
        if isPlaying { player.rate = Float(rate) }
    }

    public func pause() {
        isPlaying = false
        player.pause()
    }

    public func clear() {
        pause()
        seekRevision += 1
        loadTask?.cancel()
        itemObservation = nil
        if let atEnd { NotificationCenter.default.removeObserver(atEnd) }
        atEnd = nil
        player.replaceCurrentItem(with: nil)
        sourceID = nil
        elapsed = 0
        duration = 0
        hasPosition = false
        error = nil
    }
}

public struct MinutesAudioBar: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @ObservedObject private var playback: MinutesPlayback
    @Stored private var scrubbing = false
    @Stored private var scrubPosition = 0.0
    private let compact: Bool
    private let tint: Color
    private let motion: Animation?

    public init(playback: MinutesPlayback, compact: Bool = false, tint: Color = .primary, motion: Animation? = nil) {
        self.playback = playback
        self.compact = compact
        self.tint = tint
        self.motion = motion
    }

    public var body: some View {
        HStack(spacing: compact ? 8 : 12) {
            Button(action: playback.toggle) {
                Image(systemName: playback.isPlaying ? "pause.fill" : "play.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .contentTransition(.opacity)
                    .offset(x: playback.isPlaying ? 0 : 0.8)
                    .foregroundStyle(tint)
                    .frame(width: 36, height: 36)
                    .background(tint.opacity(0.13), in: Circle())
                    .contentShape(.circle)
            }
            .buttonStyle(.plain)
            .animation(motion, value: playback.isPlaying)
            .accessibilityLabel(playback.isPlaying ? "再生を一時停止" : "録音を再生")
            .disabled(playback.error != nil)

            if let error = playback.error {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                if !compact {
                    HStack(spacing: 3) {
                        skip("gobackward.10", by: -10, label: "10秒戻る")
                        skip("goforward.10", by: 10, label: "10秒進む")
                    }
                }
                Text(minutesTime(scrubbing ? scrubPosition : playback.elapsed))
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .accessibilityLabel("再生位置 \(minutesTime(playback.elapsed))")
                Slider(value: Binding(
                    get: { scrubbing ? scrubPosition : min(playback.elapsed, playback.duration) },
                    set: { newValue in
                        if scrubbing { scrubPosition = newValue }
                        else { playback.seek(to: newValue) }
                    }
                ), in: 0...max(playback.duration, 0.01), onEditingChanged: { editing in
                    if editing { scrubPosition = playback.elapsed }
                    else if scrubbing { playback.seek(to: scrubPosition) }
                    scrubbing = editing
                })
                .accessibilityLabel("再生位置")
                .tint(tint)
                .disabled(playback.duration <= 0)
                Text(minutesTime(playback.duration))
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                Menu {
                    if compact {
                        Button("10秒戻る") { playback.seek(to: playback.elapsed - 10) }
                        Button("10秒進む") { playback.seek(to: playback.elapsed + 10) }
                        Divider()
                    }
                    ForEach([0.5, 0.75, 1, 1.25, 1.5, 1.75, 2], id: \.self) { rate in
                        Button {
                            playback.setRate(rate)
                        } label: {
                            Label(Self.speed(rate), systemImage: playback.rate == rate ? "checkmark" : "")
                        }
                    }
                } label: {
                    Text(Self.speed(playback.rate))
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .accessibilityLabel("再生速度 \(Self.speed(playback.rate))")
            }
        }
        .padding(.leading, 6)
        .padding(.trailing, compact ? 9 : 12)
        .frame(minHeight: 48)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
        .overlay {
            RoundedRectangle(cornerRadius: 16).strokeBorder(Color.primary.opacity(0.085))
                .allowsHitTesting(false)
        }
    }

    private func skip(_ symbol: String, by delta: Double, label: String) -> some View {
        Button { playback.seek(to: playback.elapsed + delta) } label: {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .medium))
                .frame(width: 29, height: 32)
                .contentShape(Rectangle())
        }
        .buttonStyle(ReaderHoverStyle(tint: tint, motion: motion))
        .help(label)
        .accessibilityLabel(label)
    }

    private static func speed(_ rate: Double) -> String {
        "\(rate.formatted(.number.precision(.fractionLength(0...2))))×"
    }
}

public func minutesTime(_ seconds: Double) -> String {
    let total = seconds.isFinite ? Int(min(max(0, seconds), Double(Int32.max))) : 0
    if total >= 3600 {
        return String(format: "%d:%02d:%02d", total / 3600, total / 60 % 60, total % 60)
    }
    return String(format: "%02d:%02d", total / 60, total % 60)
}
