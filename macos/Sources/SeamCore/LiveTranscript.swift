import Foundation

/// One session's transcript, combining confirmed rows with the current Apple Speech preview.
public struct LiveTranscript: Sendable {
    public private(set) var sessionId: String?
    public private(set) var segments: [TranscriptSegment] = []
    private var preview: TranscriptDraft?

    public init() {}

    public var draftSegments: [TranscriptSegment] {
        guard let preview else { return [] }
        let finalizedStarts = Set(segments.map(\.start))
        let lastStart = segments.last?.start ?? -.infinity
        let lines = preview.segments ?? [
            TranscriptSegment(start: preview.start, end: preview.end ?? preview.start,
                              text: preview.text, sessionId: preview.sessionId)
        ]
        return lines.filter { $0.start >= lastStart && !finalizedStarts.contains($0.start) }
    }

    public mutating func activate(_ sessionId: String?) {
        guard sessionId != self.sessionId else { return }
        self.sessionId = sessionId
        segments = []
        preview = nil
    }

    public mutating func receive(_ incoming: TranscriptDraft) {
        guard incoming.sessionId == sessionId else { return }
        if incoming.text.isEmpty {
            if preview?.start == incoming.start { preview = nil }
        } else if incoming.start >= (preview?.start ?? -.infinity) {
            preview = incoming
        }
    }

    public mutating func receive(_ incoming: TranscriptSegment) {
        guard incoming.sessionId != nil, incoming.sessionId == sessionId else { return }
        merge([incoming])
    }

    public mutating func recover(_ saved: [TranscriptSegment], for sessionId: String) {
        guard sessionId == self.sessionId else { return }
        let existing = Set(segments.map(\.start))
        merge(saved.filter { !existing.contains($0.start) && ($0.sessionId == nil || $0.sessionId == sessionId) })
    }

    private mutating func merge(_ incoming: [TranscriptSegment]) {
        guard !incoming.isEmpty else { return }
        var byStart = Dictionary(uniqueKeysWithValues: segments.map { ($0.start, $0) })
        for segment in incoming { byStart[segment.start] = segment }
        segments = byStart.values.sorted { $0.start < $1.start }
        if draftSegments.isEmpty { preview = nil }
    }
}
