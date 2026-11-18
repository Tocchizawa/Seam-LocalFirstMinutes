/// A display transition expressed in graphemes so composed characters and emoji
/// stay intact. Previously visible text is preserved when a draft is revised.
public struct TranscriptReveal: Sendable {
    public let stableCount: Int
    public let totalCount: Int
    public let duration: Double

    public init(previous: String, next: String, maximumDuration: Double) {
        stableCount = zip(previous, next).prefix(while: { $0 == $1 }).count
        totalCount = next.count
        let added = totalCount - stableCount
        duration = added == 0 ? 0 : max(0, min(maximumDuration, Double(added) * 0.014))
    }

    public func visibleCount(elapsed: Double) -> Int {
        guard duration > 0 else { return totalCount }
        let fraction = max(0, min(1, elapsed / duration))
        let added = totalCount - stableCount
        let visible = 1 + Int(Double(added - 1) * fraction)
        return stableCount + visible
    }
}
