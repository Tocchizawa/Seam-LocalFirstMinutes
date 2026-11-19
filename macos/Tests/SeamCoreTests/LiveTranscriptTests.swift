import XCTest
@testable import SeamCore

final class LiveTranscriptTests: XCTestCase {
    func testSpeechRevisionsGrowInPlaceAndLeaveTheUncoveredTail() {
        var transcript = LiveTranscript()
        transcript.activate("current")

        transcript.receive(.init(start: 0, end: 1, text: "今", isFinal: false, parts: []), for: "current")
        transcript.receive(
            .init(start: 0, end: 3, text: "今日は晴れです", isFinal: false, parts: [
                .init(start: 0, text: "今日は"),
                .init(start: 1.5, text: "晴れです"),
            ]),
            for: "current"
        )
        XCTAssertEqual(transcript.draftSegments.map(\.text), ["今日は晴れです"])

        transcript.receive(.init(sessionId: "current", start: 0, end: 1.5, text: "今日は", segments: nil))
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
        let draft = SpeechDraftUpdate(start: 0, end: 1, text: "こんにちは", isFinal: false, parts: [])
        transcript.receive(draft, for: "first")
        XCTAssertEqual(transcript.draftSegments.count, 1)

        transcript.activate("second")
        transcript.receive(draft, for: "first")
        XCTAssertTrue(transcript.draftSegments.isEmpty)
    }
}
