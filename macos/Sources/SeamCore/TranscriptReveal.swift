/// Schedules only the changed suffix. Unsettled characters in the common
/// prefix keep their original timing when another partial result arrives.
public struct TranscriptReveal: Sendable {
    public struct Cue: Hashable, Sendable {
        public let start: Double
        public let settle: Double

        public func progress(at time: Double) -> Double {
            min(1, max(0, (time - start) / (settle - start)))
        }
    }

    public private(set) var text = ""
    public private(set) var cues: [Int: Cue] = [:]

    public init() {}

    public var settlesAt: Double? { cues.values.map(\.settle).max() }

    public mutating func update(_ next: String, at time: Double, window: Double) {
        let shared = zip(text, next).prefix(while: { $0 == $1 }).count
        text = next
        guard window > 0 else {
            finish()
            return
        }

        cues = cues.filter { $0.key < shared && $0.value.settle > time }
        let added = next.count - shared
        guard added > 0 else { return }
        // Backfill or a large correction should be readable immediately.
        guard added <= 96 else {
            finish()
            return
        }

        let landingDuration = min(0.185, window * 0.72)
        let latestStart = cues.values.map(\.start).max() ?? time
        let firstStart = max(time, latestStart)
        let availableSpread = max(0, time + window - landingDuration - firstStart)
        let spacing = added > 1 ? min(0.023, availableSpread / Double(added - 1)) : 0
        for offset in 0..<added {
            let start = firstStart + Double(offset) * spacing
            cues[shared + offset] = Cue(start: start, settle: start + landingDuration)
        }
    }

    public mutating func finish() { cues.removeAll() }
}
