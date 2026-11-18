import SwiftUI

public struct MinutesSearchField: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var focused: Bool
    @Binding private var expanded: Bool
    @Binding private var query: String

    private let width: CGFloat
    private let animated: Bool
    private let onOpen: () -> Void
    private let onEdit: () -> Void
    private let control: CGFloat = 36

    public init(expanded: Binding<Bool>, query: Binding<String>, width: CGFloat = 230, animated: Bool = true,
                onOpen: @escaping () -> Void, onEdit: @escaping () -> Void) {
        self._expanded = expanded
        self._query = query
        self.width = max(120, width)
        self.animated = animated
        self.onOpen = onOpen
        self.onEdit = onEdit
    }

    private var motion: Animation? { animated && !reduceMotion ? .spring(response: 0.28, dampingFraction: 0.91) : nil }
    private var searchText: Binding<String> {
        Binding(get: { query }, set: { query = $0; onEdit() })
    }

    public var body: some View {
        surface(
            HStack(spacing: 0) {
                TextField("タイトル・本文・要約", text: searchText)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
                    .focused($focused)
                    .accessibilityLabel("議事録を検索")
                    .onExitCommand(perform: collapse)
                    .padding(.leading, 12)
                    .frame(width: expanded ? width - control : 0)
                    .clipped()
                    .opacity(expanded ? 1 : 0)
                    .disabled(!expanded)
                    .accessibilityHidden(!expanded)

                Button {
                    if expanded { collapse() }
                    else {
                        withAnimation(motion) {
                            onOpen()
                            expanded = true
                        }
                    }
                } label: {
                    icon
                        .font(.system(size: 14, weight: .medium))
                        .frame(width: control, height: control)
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(expanded ? "検索を閉じる" : "検索")
                .help(expanded ? "検索を閉じる Esc" : "議事録を検索 ⌘F")
                .keyboardShortcut(expanded ? nil : KeyboardShortcut("f", modifiers: .command))
            }
            .frame(width: expanded ? width : control, height: control, alignment: .trailing)
            .fixedSize()
        )
        .animation(motion, value: expanded)
        .task(id: expanded) {
            if expanded {
                await Task.yield()
                if !Task.isCancelled { focused = true }
            }
        }
    }

    private func collapse() {
        focused = false
        withAnimation(motion) {
            expanded = false
            query = ""
        }
    }

    @ViewBuilder
    private var icon: some View {
        let mark = Image(systemName: expanded ? "xmark" : "magnifyingglass")
        if #available(macOS 14.0, *) {
            mark.contentTransition(reduceMotion || !animated ? .identity : .symbolEffect(.replace))
        } else {
            mark
        }
    }

    @ViewBuilder
    private func surface<Content: View>(_ view: Content) -> some View {
        #if compiler(>=6.2)
        if #available(macOS 26.0, *) {
            view.glassEffect(.regular.interactive(), in: .capsule)
        } else {
            view.background(.regularMaterial, in: Capsule())
        }
        #else
        view.background(.regularMaterial, in: Capsule())
        #endif
    }
}
