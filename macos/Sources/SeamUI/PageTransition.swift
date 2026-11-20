import SwiftUI

/// ページ切り替え時の短い縦移動。距離と時間は keyframe の値で調整する。
@available(macOS 15.0, *)
public extension View {
    @ViewBuilder
    func animatePageChange(for page: some Equatable, enabled: Bool) -> some View {
        if enabled {
            keyframeAnimator(initialValue: CGFloat.zero, trigger: page) { content, offset in
                content.offset(y: offset)
            } keyframes: { _ in
                MoveKeyframe(CGFloat(4))
                SpringKeyframe(CGFloat.zero, duration: 0.23, spring: .smooth(duration: 0.20))
            }
            .clipped()
        } else {
            self
        }
    }
}
