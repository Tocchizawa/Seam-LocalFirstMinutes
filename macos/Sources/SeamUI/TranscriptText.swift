import SwiftUI
#if SWIFT_PACKAGE
import SeamCore
#endif

/// Animates the glyphs added by each live revision.
/// Full text is measured up front so the reveal doesn't reflow the paragraph.
public struct TranscriptText: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let text: String
    private let duration: Double

    public init(_ text: String, duration: Double = 0.31) {
        self.text = text
        self.duration = duration
    }

    public var body: some View {
        #if compiler(>=6.0)
        if #available(macOS 15.0, *) {
            AnimatedTranscript(text: text, window: reduceMotion ? 0 : duration)
        } else {
            Text(verbatim: text)
        }
        #else
        Text(verbatim: text)
        #endif
    }
}

#if compiler(>=6.0)
@available(macOS 15.0, *)
private struct AnimatedTranscript: View {
    // The alias also works with installations of Command Line Tools that
    // provide the SwiftUI property wrapper but lack the State macro plugin.
    private typealias Stored<Value> = SwiftUI.State<Value>

    let text: String
    let window: Double
    @Stored private var scheduled = TranscriptReveal()

    private struct Revision: Equatable {
        let text: String
        let window: Double
    }

    var body: some View {
        let revision = Revision(text: text, window: window)
        rendered
            .task(id: revision) {
                scheduled.update(text, at: ProcessInfo.processInfo.systemUptime, window: window)
                guard let end = scheduled.settlesAt else { return }
                let delay = max(0, end - ProcessInfo.processInfo.systemUptime)
                do { try await Task.sleep(for: .seconds(delay)) }
                catch { return }
                guard !Task.isCancelled else { return }
                scheduled.finish()
            }
    }

    @ViewBuilder
    private var rendered: some View {
        if window <= 0 || (scheduled.text == text && scheduled.cues.isEmpty) {
            Text(verbatim: text)
        } else {
            let marked = markedText()
            ZStack(alignment: .topLeading) {
                TimelineView(.animation(minimumInterval: 1.0 / 60.0)) { _ in
                    marked.textRenderer(GlyphLanding(now: ProcessInfo.processInfo.systemUptime))
                }
                .textSelection(.disabled)
                .accessibilityHidden(true)
                .allowsHitTesting(false)

                // The native text remains the accessible and selectable layer.
                Text(verbatim: text).foregroundStyle(.clear)
            }
        }
    }

    private func markedText() -> Text {
        var motion = scheduled
        if motion.text != text {
            motion.update(text, at: ProcessInfo.processInfo.systemUptime, window: window)
        }

        var result = Text(verbatim: "")
        var unmarked = ""
        for (index, character) in text.enumerated() {
            if let cue = motion.cues[index] {
                if !unmarked.isEmpty {
                    result = Text("\(result)\(Text(verbatim: unmarked))")
                    unmarked.removeAll(keepingCapacity: true)
                }
                let letter = Text(verbatim: String(character)).customAttribute(GlyphTiming(cue: cue))
                result = Text("\(result)\(letter)")
            } else {
                unmarked.append(character)
            }
        }
        return Text("\(result)\(Text(verbatim: unmarked))")
    }
}

@available(macOS 15.0, *)
private struct GlyphTiming: TextAttribute {
    let cue: TranscriptReveal.Cue
}

@available(macOS 15.0, *)
private struct GlyphLanding: TextRenderer {
    let now: Double

    var displayPadding: EdgeInsets { EdgeInsets(top: 3, leading: 3, bottom: 6, trailing: 3) }

    func draw(layout: Text.Layout, in context: inout GraphicsContext) {
        for line in layout {
            for run in line {
                guard let timing = run[GlyphTiming.self] else {
                    context.draw(run)
                    continue
                }
                guard now >= timing.cue.start else { continue }
                let progress = timing.cue.progress(at: now)
                if progress == 1 {
                    context.draw(run)
                    continue
                }

                let distance = pow(1 - progress, 3)
                var glyphContext = context
                glyphContext.opacity = 1 - 0.84 * distance
                glyphContext.translateBy(x: 0, y: 3.2 * distance)
                glyphContext.addFilter(.blur(radius: 2.3 * distance))
                glyphContext.draw(run)
            }
        }
    }
}
#endif
