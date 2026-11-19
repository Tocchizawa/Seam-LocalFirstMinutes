import SwiftUI

@available(macOS 15.0, *)
public extension View {
    /// ウィンドウ上端の共通操作バー。高さや左右の余白は下の frame/padding で調整する。
    func workspaceTitleBar<Actions: View>(@ViewBuilder actions: () -> Actions) -> some View {
        scrollEdgeBar(blur: false) {
            HStack(spacing: 12) {
                Spacer(minLength: 76)
                actions()
            }
            .frame(height: 36)
            .padding([.top, .trailing], 10)
            .padding(.bottom, 8)
            .background {
                Color.clear
                    .contentShape(Rectangle())
                    .gesture(WindowDragGesture())
            }
        }
        .ignoresSafeArea(.container, edges: .top)
    }
}
