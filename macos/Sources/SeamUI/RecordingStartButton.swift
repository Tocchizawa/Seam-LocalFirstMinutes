import SwiftUI

public struct RecordingStartButton: View {
    private let action: () -> Void

    public init(action: @escaping () -> Void) { self.action = action }

    public var body: some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: "record.circle.fill")
                    .accessibilityHidden(true)
                Text("録音開始")
            }
            .font(.system(size: 13, weight: .semibold))
            .fixedSize(horizontal: true, vertical: false)
        }
        .buttonStyle(RecordingStartStyle())
        .keyboardShortcut("r", modifiers: [.command, .shift])
        .help("録音開始 ⇧⌘R")
    }
}

private struct RecordingStartStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed
        let red = Color(.sRGB, red: 0.70, green: 0.12, blue: 0.21, opacity: 1)
        configuration.label
            .padding(.horizontal, 14)
            .frame(height: 36)
            .foregroundStyle(Color.white)
            .background {
                Capsule()
                    .fill(red)
                    .overlay {
                        Capsule().fill(.black.opacity(pressed ? 0.14 : 0))
                    }
                    .overlay {
                        Capsule().strokeBorder(.white.opacity(0.12))
                    }
            }
            .opacity(enabled ? 1 : 0.42)
            .scaleEffect(pressed && !reduceMotion ? 0.97 : 1)
            .contentShape(Capsule())
            .animation(reduceMotion ? nil : .easeOut(duration: 0.13), value: pressed)
    }
}
