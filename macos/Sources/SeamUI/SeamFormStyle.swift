import SwiftUI

package extension View {
    @ViewBuilder
    func seamActionPointer(_ enabled: Bool) -> some View {
        if #available(macOS 15.0, *) { pointerStyle(enabled ? .link : .default) }
        else { self }
    }

    @ViewBuilder
    func seamTextPointer() -> some View {
        if #available(macOS 15.0, *) { pointerStyle(.horizontalText) }
        else { self }
    }

    /// フォームとチャットで共通の、読み書きに使う面。
    func seamFieldSurface(focused: Bool = false, radius: CGFloat = 12) -> some View {
        background(Color.primary.opacity(focused ? 0.045 : 0.033), in: .rect(cornerRadius: radius))
            .overlay {
                RoundedRectangle(cornerRadius: radius)
                    .strokeBorder(Color.primary.opacity(focused ? 0.22 : 0.10), lineWidth: 1)
                    .allowsHitTesting(false)
            }
    }
}

package struct SeamFormActionStyle: ButtonStyle {
    let primary: Bool
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.isEnabled) private var enabled
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    package init(primary: Bool = false) { self.primary = primary }

    package func makeBody(configuration: Configuration) -> some View {
        let emphasized = primary && enabled
        let foreground = emphasized ? Color.seamPaper(for: colorScheme) : Color.primary
        let label = configuration.label
            .font(.system(size: 12, weight: .semibold))
            .padding(.horizontal, 12)
            .frame(minHeight: 32)
            .foregroundStyle(foreground.opacity(enabled ? 1 : 0.44))
            .contentShape(Capsule())
        Group {
            if #available(macOS 26.0, *) {
                label.glassEffect(emphasized ? .regular.tint(.primary.opacity(0.88)).interactive() :
                    .regular.interactive(enabled), in: .capsule)
            } else {
                label.background(Color.primary.opacity(emphasized ? 0.88 : 0.075), in: .capsule)
            }
        }
        .seamActionPointer(enabled)
        .scaleEffect(configuration.isPressed && !reduceMotion ? 0.96 : 1)
        .animation(reduceMotion ? nil : SeamMotion.feedback, value: configuration.isPressed)
    }
}
