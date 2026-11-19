import SwiftUI

public extension View {
    @ViewBuilder
    func scrollEdgeBar<Bar: View>(_ edge: VerticalEdge = .top, blur: Bool = true,
                                  @ViewBuilder bar: () -> Bar) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            if edge == .top && blur {
                modifier(ScrollingHeader(bar: bar()))
            } else {
                safeAreaBar(edge: edge, spacing: 0) { bar() }
                    .scrollEdgeEffectStyle(.soft, for: edge == .top ? .top : .bottom)
            }
        } else {
            safeAreaInset(edge: edge, spacing: 0) { bar().background(.bar) }
        }
        #else
        safeAreaInset(edge: edge, spacing: 0) { bar().background(.bar) }
        #endif
    }
}

#if compiler(>=6.2)
@available(macOS 26.0, *)
private struct ScrollingHeader<Bar: View>: ViewModifier {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.colorScheme) private var colorScheme
    @Stored private var scrollDepth = 0.0
    let bar: Bar

    func body(content: Content) -> some View {
        content
            .onScrollGeometryChange(for: Double.self) { geometry in
                let distance = Double(geometry.contentOffset.y + geometry.contentInsets.top)
                return min(max(distance, 0), 24) / 24
            } action: { _, depth in
                scrollDepth = depth
            }
            .safeAreaBar(edge: .top, spacing: 0) {
                bar.background {
                    let paper = Color(white: colorScheme == .dark ? 0.087 : 0.991)
                    Rectangle()
                        .fill(.regularMaterial)
                        .overlay(paper.opacity(0.74))
                        .mask(alignment: .top) {
                            LinearGradient(stops: [
                                .init(color: .black, location: 0.58),
                                .init(color: .black.opacity(0.92), location: 0.72),
                                .init(color: .clear, location: 1)
                            ], startPoint: .top, endPoint: .bottom)
                        }
                        .opacity(scrollDepth)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
            .scrollEdgeEffectStyle(.soft, for: .top)
    }
}
#endif
