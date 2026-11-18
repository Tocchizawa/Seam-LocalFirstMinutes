import SwiftUI

@available(macOS 15.0, *)
public extension View {
    func workspaceTitleBar<Actions: View>(@ViewBuilder actions: () -> Actions) -> some View {
        scrollEdgeBar {
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
