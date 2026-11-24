import SeamUI
import SwiftUI

@available(macOS 26.0, *)
struct MeetingSplitView<Main: View, Side: View>: View {
    let showing: Bool
    var overlaysWhenNarrow = false
    @Binding var preferredWidth: Double
    @ViewBuilder let main: () -> Main
    @ViewBuilder let side: (_ overlay: Bool) -> Side

    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.displayScale) private var displayScale
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @GestureState private var dragOffset: CGFloat = 0
    @State private var hovering = false

    var body: some View {
        GeometryReader { geometry in
            let space = geometry.size.width
            let overlay = overlaysWhenNarrow && space < 580
            let maxWidth = max(0, overlay ? space - 36 : space - 308)
            let minWidth = min(248, maxWidth)
            let defaultWidth = overlay ? space * 0.76 : min(420, space * 0.43)
            let restingWidth = min(maxWidth, max(minWidth, preferredWidth > 0 ? preferredWidth : defaultWidth))
            let width = min(maxWidth, max(minWidth, restingWidth - dragOffset))
            main()
                .frame(width: max(0, showing && !overlay ? space - width : space))
                .frame(maxWidth: .infinity, alignment: .leading)
                .disabled(showing && overlay)
                .overlay(alignment: .trailing) {
                    if showing {
                        side(overlay)
                            .frame(width: width)
                            .background(Color.seamPaper(for: colorScheme))
                            .clipped()
                            .overlay(alignment: .leading) {
                                resizeHandle(width: width, resting: restingWidth, limits: minWidth...maxWidth)
                            }
                            .transition(reduceMotion ? .identity : .offset(x: 16).combined(with: .opacity))
                    }
                }
                .clipped()
                .animation(reduceMotion ? nil : SeamMotion.reposition, value: showing)
        }
    }

    private func resizeHandle(width: CGFloat, resting: CGFloat, limits: ClosedRange<CGFloat>) -> some View {
        let highlighted = hovering || dragOffset != 0
        let canResize = limits.lowerBound < limits.upperBound
        return Rectangle()
            .fill(Color.primary.opacity(highlighted && canResize ? 0.34 : 0.12))
            .frame(width: 1 / displayScale)
            .frame(width: 12, alignment: .leading)
            .contentShape(Rectangle())
            .pointerStyle(canResize ? .columnResize : .default)
            .allowsHitTesting(canResize)
            .onHover { hovering = $0 }
            .animation(reduceMotion ? nil : SeamMotion.feedback, value: highlighted)
            .gesture(
                DragGesture(minimumDistance: 1, coordinateSpace: .global)
                    .updating($dragOffset) { value, offset, _ in offset = value.translation.width }
                    .onEnded { value in preferredWidth = Double(clamp(resting - value.translation.width, to: limits)) }
            )
            .accessibilityLabel("右パネルの幅")
            .accessibilityValue("\(Int(width))ポイント")
            .accessibilityHidden(!canResize)
            .accessibilityAdjustableAction { direction in
                switch direction {
                case .increment: preferredWidth = Double(clamp(width + 24, to: limits))
                case .decrement: preferredWidth = Double(clamp(width - 24, to: limits))
                @unknown default: break
                }
            }
    }

    private func clamp(_ width: CGFloat, to range: ClosedRange<CGFloat>) -> CGFloat {
        min(range.upperBound, max(range.lowerBound, width))
    }
}
