import SwiftUI

@available(macOS 26.0, *)
public extension View {
    /// メインウインドウのヘッダーにタイトルと操作を表示する。
    func workspaceTitleBar<Actions: View>(title: String, subtitle: String?, sidebarVisible: Bool, recording: Bool,
                                         toggleSidebar: @escaping () -> Void,
                                         @ViewBuilder actions: () -> Actions) -> some View {
        modifier(WorkspaceTitleBar(title: title, subtitle: subtitle, sidebarVisible: sidebarVisible,
                                   recording: recording, toggleSidebar: toggleSidebar, actions: actions()))
    }
}

@available(macOS 26.0, *)
private struct WorkspaceTitleBar<Actions: View>: ViewModifier {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.displayScale) private var displayScale
    let title: String
    let subtitle: String?
    let sidebarVisible: Bool
    let recording: Bool
    let toggleSidebar: () -> Void
    let actions: Actions

    private var leadingGroup: some View {
        let label = sidebarVisible ? "プロジェクトパネルを閉じる" : "プロジェクトパネルを開く"
        return HStack(spacing: 12) {
            Button(action: toggleSidebar) {
                Image(systemName: "sidebar.left")
                    .font(.system(size: 16, weight: .medium))
                    .frame(width: 36, height: 36)
                    .contentShape(Circle())
                    .glassEffect(.regular.interactive(), in: .circle)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(label)
            .help(label)

            heading
        }
    }

    private var heading: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(verbatim: title)
                .font(.system(size: 13, weight: .semibold))
            if let subtitle {
                Text(verbatim: subtitle)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
        .lineLimit(1)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
        .help(title)
    }

    private var barContents: some View {
        HStack(spacing: 12) {
            leadingGroup
            Spacer(minLength: 0)
            HStack(spacing: 8) { actions }
                .fixedSize(horizontal: true, vertical: false)
        }
        .animation(reduceMotion ? nil : SeamMotion.reposition, value: recording)
    }

    func body(content: Content) -> some View {
        let isPreview = ProcessInfo.processInfo.environment["XCODE_RUNNING_FOR_PREVIEWS"] == "1"
        return content
            .safeAreaInset(edge: .top, spacing: 0) {
                barContents
                    .padding(.leading, isPreview ? 16 : 87)
                    .padding(.trailing, 8)
                    .frame(height: 52)
                    .background {
                        Color.seamPanel(for: colorScheme)
                            .contentShape(Rectangle())
                            .gesture(WindowDragGesture())
                    }
                    .overlay(alignment: .bottom) {
                        Rectangle()
                            .fill(Color(nsColor: .separatorColor))
                            .frame(height: 1 / displayScale)
                            .allowsHitTesting(false)
                            .accessibilityHidden(true)
                    }
            }
            .ignoresSafeArea(.container, edges: .top)
    }
}
