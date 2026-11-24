import Foundation

public struct MeetingNote: Decodable, Identifiable, Sendable {
    public let id: Int
    public let start: Double
    public let title: String
    public let customTitle: String?
    public let body: String
}

public struct MeetingNotesUpdate: Decodable, Sendable {
    public let notes: [MeetingNote]
    public let summaryQueued: Bool
}

public struct MeetingChat: Decodable, Identifiable, Sendable {
    public let id: String
    public let title: String?
}

public struct MeetingChatMessage: Decodable, Sendable {
    public enum Role: String, Decodable, Sendable { case user, assistant }
    public let role: Role
    public let content: String
}

public struct MeetingConversation: Decodable, Sendable {
    public let id: String
    public let title: String?
    public let messages: [MeetingChatMessage]
}

public extension Array where Element == TranscriptSegment {
    func transcriptIndex(at time: Double) -> Int? {
        guard time.isFinite, time >= 0, !isEmpty else { return nil }
        // 発言は時刻順。再文字起こしで時刻がずれた場合は、その時点に最も近い発言へ移動する。
        let next = partitionIndex { $0.start >= time }
        if next == 0 { return 0 }
        if next == count { return count - 1 }
        if self[next].start == time { return next }
        let previous = next - 1
        if self[previous].end > time { return previous }
        return time - self[previous].start <= self[next].start - time ? previous : next
    }

    private func partitionIndex(where belongsInUpperHalf: (Element) -> Bool) -> Int {
        var lower = 0
        var upper = count
        while lower < upper {
            let middle = (lower + upper) / 2
            if belongsInUpperHalf(self[middle]) { upper = middle }
            else { lower = middle + 1 }
        }
        return lower
    }
}
