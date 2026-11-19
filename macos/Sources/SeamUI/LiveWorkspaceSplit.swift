import SwiftUI

/// Keeps the same transcript view mounted while the reader expands it or
/// returns to the archive. The archive uses the full width when capture ends.
public struct LiveWorkspaceSplit<Archive: View, Transcript: View>: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    private let recording: Bool
    private let expanded: Bool
    private let animated: Bool
    private let archive: (_ compact: Bool, _ narrow: Bool) -> Archive
    private let transcript: (_ expanded: Bool) -> Transcript

    public init(recording: Bool, expanded: Bool, animated: Bool = true,
                @ViewBuilder archive: @escaping (Bool, Bool) -> Archive,
                @ViewBuilder transcript: @escaping (Bool) -> Transcript) {
        self.recording = recording
        self.expanded = expanded
        self.animated = animated
        self.archive = archive
        self.transcript = transcript
    }

    public var body: some View {
        GeometryReader { geometry in
            let full = recording && expanded
            let compactTrack = min(336, geometry.size.width * 0.355)
            let archiveWidth = max(0, geometry.size.width - (recording ? compactTrack : 0))

            HStack(spacing: 0) {
                archive(recording, geometry.size.width < 920)
                    .frame(width: archiveWidth, height: geometry.size.height)
                    .frame(width: full ? 0 : archiveWidth, alignment: .leading)
                    .clipped()
                    .opacity(full ? 0 : 1)
                    .allowsHitTesting(!full)
                    .disabled(full)
                    .accessibilityElement(children: .contain)
                    .accessibilityHidden(full)

                if recording {
                    transcript(full)
                        .frame(width: full ? geometry.size.width : compactTrack, height: geometry.size.height)
                        .background(Color.seamPaper(for: colorScheme))
                        .clipped()
                        .transition(motion == nil ? .identity : .move(edge: .trailing))
                        .zIndex(1)
                }
            }
            .animation(motion, value: recording)
            .animation(motion, value: full)
        }
    }

    private var motion: Animation? {
        reduceMotion || !animated ? nil : SeamMotion.reposition
    }
}
