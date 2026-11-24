import XCTest
@testable import SeamCore

final class LiveTranscriptTests: XCTestCase {
    func testSpeechRevisionsGrowInPlaceAndLeaveTheUncoveredTail() {
        var transcript = LiveTranscript()
        transcript.activate("current")

        transcript.receive(TranscriptDraft(sessionId: "current", start: 0, end: 1, text: "今", segments: nil))
        transcript.receive(TranscriptDraft(sessionId: "current", start: 0, end: 3, text: "今日は晴れです", segments: nil))
        XCTAssertEqual(transcript.draftSegments.map(\.text), ["今日は晴れです"])

        transcript.receive(TranscriptDraft(sessionId: "current", start: 0, end: 3, text: "今日は晴れです", segments: [
            .init(start: 0, end: 1.5, text: "今日は"),
            .init(start: 1.5, end: 3, text: "晴れです"),
        ]))
        XCTAssertEqual(transcript.draftSegments.map(\.text), ["今日は", "晴れです"])

        transcript.recover([.init(start: 0, end: 1.5, text: "今日は")], for: "current")
        XCTAssertEqual(transcript.segments.map(\.text), ["今日は"])
        XCTAssertEqual(transcript.draftSegments.map(\.text), ["晴れです"])
        XCTAssertEqual(transcript.draftSegments.first?.start, 1.5)

        transcript.receive(.init(start: 1.5, end: 3, text: "晴れです。", sessionId: "current"))
        XCTAssertTrue(transcript.draftSegments.isEmpty)
    }

    func testSpeechDraftsBelongToOneRecording() {
        var transcript = LiveTranscript()
        transcript.activate("first")
        let draft = TranscriptDraft(sessionId: "first", start: 0, end: 1, text: "こんにちは", segments: nil)
        transcript.receive(draft)
        XCTAssertEqual(transcript.draftSegments.count, 1)

        transcript.activate("second")
        transcript.receive(draft)
        XCTAssertTrue(transcript.draftSegments.isEmpty)
    }

    func testTranscriptJumpFindsExactOrClosestRow() {
        let rows = [
            TranscriptSegment(start: 1, end: 4, text: "最初の発言"),
            TranscriptSegment(start: 5, end: 8, text: "次の発言"),
        ]
        XCTAssertEqual(rows.transcriptIndex(at: 5), 1)
        XCTAssertEqual(rows.transcriptIndex(at: 3.8), 0)
        XCTAssertEqual(rows.transcriptIndex(at: 4.4), 1)
        XCTAssertNil(rows.transcriptIndex(at: .infinity))
    }
}
