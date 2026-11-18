import SwiftUI

public extension View {
    @ViewBuilder
    func scrollEdgeBar<Bar: View>(_ edge: VerticalEdge = .top, @ViewBuilder bar: () -> Bar) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            safeAreaBar(edge: edge, spacing: 0) {
                bar()
            }
            .scrollEdgeEffectStyle(.soft, for: edge == .top ? .top : .bottom)
        } else {
            safeAreaInset(edge: edge, spacing: 0) { bar().background(.bar) }
        }
        #else
        safeAreaInset(edge: edge, spacing: 0) { bar().background(.bar) }
        #endif
    }
}
