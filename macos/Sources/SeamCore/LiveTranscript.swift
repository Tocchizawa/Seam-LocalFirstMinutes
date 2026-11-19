import Foundation

/// One session's transcript. Whisper fills in confirmed rows while Apple
/// Speech supplies the portion of the live text that Whisper has yet to cover.
public struct LiveTranscript: Sendable {
    public private(set) var sessionId: String?
    public private(set) var segments: [TranscriptSegment] = []
    private var preview: TranscriptDraft?
    private var speechDrafts: [SpeechDraftUpdate] = []

    public init() {}

    public var draftSegments: [TranscriptSegment] {
        let whisper = whisperDraftSegments
        let coveredThrough = max(segments.last?.end ?? 0, whisper.last?.end ?? 0)
        let tail = speechDrafts.compactMap { draft -> TranscriptSegment? in
            guard draft.end > coveredThrough else { return nil }
            if draft.start >= coveredThrough {
                return TranscriptSegment(start: draft.start, end: draft.end, text: draft.text, sessionId: sessionId)
            }
            let parts = draft.parts.drop(while: { $0.start < coveredThrough })
            guard let start = parts.first?.start else { return nil }
            let text = parts.map(\.text).joined().trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { return nil }
            return TranscriptSegment(start: start, end: draft.end, text: text, sessionId: sessionId)
        }
        return whisper + tail
    }

    private var whisperDraftSegments: [TranscriptSegment] {
        guard let preview else { return [] }
        let finalizedStartTimes = Set(segments.map(\.start))
        let lastStart = segments.last?.start ?? -.infinity
        let lines = preview.segments ?? [
            TranscriptSegment(start: preview.start, end: preview.end ?? preview.start, text: preview.text,
                              speakerId: nil, speakerLabel: nil, sessionId: preview.sessionId)
        ]
        return lines.filter { $0.start >= lastStart && !finalizedStartTimes.contains($0.start) }
    }

    public mutating func activate(_ sessionId: String?) {
        guard sessionId != self.sessionId else { return }
        self.sessionId = sessionId
        segments = []
        preview = nil
        speechDrafts = []
    }

    public mutating func receive(_ incoming: SpeechDraftUpdate, for sessionId: String) {
        guard sessionId == self.sessionId, incoming.end > (segments.last?.end ?? 0) else { return }
        // A volatile phrase is revised until SpeechTranscriber finalizes it.
        speechDrafts.removeAll {
            !$0.isFinal && $0.start < incoming.end && $0.end > incoming.start
        }
        speechDrafts.append(incoming)
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
        if whisperDraftSegments.isEmpty { preview = nil }
        if let coveredThrough = segments.last?.end {
            speechDrafts.removeAll { $0.end <= coveredThrough }
        }
    }
}
