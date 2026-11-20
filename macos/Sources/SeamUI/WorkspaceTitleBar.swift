import AppKit
import SwiftUI

@available(macOS 26.0, *)
public extension View {
    /// メインウインドウのヘッダーにタイトルと操作を表示する。
    func workspaceTitleBar<Actions: View>(projectID: String?, title: String, subtitle: String?, sidebarVisible: Bool, recording: Bool,
                                         toggleSidebar: @escaping () -> Void,
                                         @ViewBuilder actions: () -> Actions) -> some View {
        modifier(WorkspaceTitleBar(projectID: projectID, title: title, subtitle: subtitle, sidebarVisible: sidebarVisible,
                                   recording: recording, toggleSidebar: toggleSidebar, actions: actions()))
    }
}

@available(macOS 26.0, *)
private struct WorkspaceTitleBar<Actions: View>: ViewModifier {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.displayScale) private var displayScale
    let projectID: String?
    let title: String
    let subtitle: String?
    let sidebarVisible: Bool
    let recording: Bool
    let toggleSidebar: () -> Void
    let actions: Actions

    private var sidebarButton: some View {
        let label = sidebarVisible ? "プロジェクトパネルを閉じる" : "プロジェクトパネルを開く"
        return Button(action: toggleSidebar) {
            Image(systemName: "sidebar.left")
                .font(.system(size: 16, weight: .medium))
                .frame(width: 36, height: 36)
                .glassEffect(.regular.interactive(), in: .circle)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .allowsWindowActivationEvents(true)
        .accessibilityLabel(label)
        .help(label)
    }

    private var heading: some View {
        ZStack(alignment: .leading) {
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: title)
                    .font(.system(size: 13, weight: .semibold))
                Text(verbatim: subtitle ?? "")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .contentTransition(reduceMotion ? .identity : .numericText())
                    .opacity(subtitle == nil ? 0 : 1)
                    .accessibilityHidden(subtitle == nil)
            }
            .lineLimit(1)
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isHeader)
            .id(projectID)
            .transition(reduceMotion ? .identity : .asymmetric(
                insertion: .opacity.combined(with: .offset(y: 5)),
                removal: .opacity.combined(with: .offset(y: -5))
            ))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .animation(reduceMotion ? nil : SeamMotion.reposition, value: projectID)
        .animation(reduceMotion ? nil : SeamMotion.update, value: subtitle)
        .background { TitleBarDragArea().accessibilityHidden(true) }
    }

    private var barContents: some View {
        HStack(spacing: 12) {
            sidebarButton
            heading
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
                    .background(Color.seamPanel(for: colorScheme))
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

private struct TitleBarDragArea: NSViewRepresentable {
    func makeNSView(context: Context) -> DragView { DragView() }
    func updateNSView(_ view: DragView, context: Context) {}

    final class DragView: NSView {
        private var dragStart: NSEvent?

        override var mouseDownCanMoveWindow: Bool { false }
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

        override func mouseDown(with event: NSEvent) {
            dragStart = nil
            if event.clickCount == 2 {
                guard let window, window.styleMask.contains(.resizable),
                      !window.styleMask.contains(.fullScreen) else { return }
                window.performZoom(nil)
            } else if event.clickCount == 1 {
                dragStart = event
            }
        }

        override func mouseDragged(with event: NSEvent) {
            guard let dragStart else { return }
            self.dragStart = nil
            window?.performDrag(with: dragStart)
        }

        override func mouseUp(with event: NSEvent) { dragStart = nil }
    }
}
