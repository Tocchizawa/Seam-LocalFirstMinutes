import SwiftUI

public extension Color {
    /// ウィンドウ背景色。ライト/ダークの基準色はここで調整する。
    static func seamPaper(for scheme: ColorScheme) -> Color {
        Color(white: scheme == .dark ? 0.087 : 0.991)
    }

    /// ウインドウのヘッダーとサイドバーで使う背景色。
    @available(macOS 26.0, *)
    package static func seamPanel(for scheme: ColorScheme) -> Color {
        let tint = Color(white: scheme == .dark ? 0.125 : 0.970)
        return seamPaper(for: scheme).mix(with: tint, by: 0.5)
    }
}

public extension View {
    /// スクロール領域に重なる上端/下端バー。blur を false にするとぼかしを外す。
    @ViewBuilder
    func scrollEdgeBar<Bar: View>(_ edge: VerticalEdge = .top, blur: Bool = true,
                                  @ViewBuilder bar: () -> Bar) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            if edge == .top && blur {
                safeAreaBar(edge: .top, spacing: 0) {
                    bar().background { HeaderFade() }
                }
                .scrollEdgeEffectStyle(.soft, for: .top)
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

private struct HeaderFade: View {
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        let paper = Color.seamPaper(for: colorScheme)
        GeometryReader { geometry in
            if geometry.size.height > 0 {
                paper.overlay(alignment: .bottom) {
                    LinearGradient(colors: [paper, paper.opacity(0)], startPoint: .top, endPoint: .bottom)
                        .frame(height: 16)
                        .offset(y: 16)
                }
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}
