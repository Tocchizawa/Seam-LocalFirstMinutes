import SwiftUI

/// 一覧ヘッダーの検索アイコンと入力欄。初期幅は init の width で指定する。
public struct MinutesSearchField: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @FocusState private var focused: Bool
    @Binding private var expanded: Bool
    @Binding private var query: String
    @State private var hidingCloseButton = false

    private let width: CGFloat
    private let animated: Bool
    private let control: CGFloat = 36

    public init(expanded: Binding<Bool>, query: Binding<String>, width: CGFloat = 230, animated: Bool = true) {
        self._expanded = expanded
        self._query = query
        self.width = max(120, width)
        self.animated = animated
    }

    private var motion: Animation? { animated && !reduceMotion ? SeamMotion.reposition : nil }
    private var hasFilter: Bool { !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    public var body: some View {
        // 入力欄は開閉に合わせて幅を変える。control は常に表示するボタン部分の幅。
        surface(
            HStack(spacing: 0) {
                TextField("タイトル・本文・要約", text: $query)
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
                    else { expanded = true }
                }
                label: {
                    icon
                        .font(.system(size: 14, weight: .medium))
                        .frame(width: control, height: control)
                        .contentShape(.rect)
                }
                .buttonStyle(.plain)
                .opacity(hidingCloseButton ? 0 : 1)
                .accessibilityHidden(hidingCloseButton)
                .accessibilityLabel(expanded ? "検索をクリア" : (hasFilter ? "検索条件を編集" : "検索"))
                .help(expanded ? "検索をクリア Esc" : (hasFilter ? "検索条件を編集 ⌘F" : "議事録を検索 ⌘F"))
                .keyboardShortcut(expanded ? nil : KeyboardShortcut("f", modifiers: .command))
            }
            .frame(width: expanded ? width : control, height: control, alignment: .trailing)
            .fixedSize()
        )
        .animation(motion, value: expanded)
        .onChange(of: focused) { hasFocus in
            if !hasFocus && expanded { closeSearch() }
        }
        .task(id: expanded) {
            if expanded {
                await Task.yield()
                if !Task.isCancelled { focused = true }
            }
        }
        .task(id: hidingCloseButton) {
            guard hidingCloseButton else { return }
            if motion != nil {
                do { try await Task.sleep(for: .milliseconds(70)) }
                catch { return }
            }
            expanded = false
            hidingCloseButton = false
        }
    }

    private func collapse() {
        query = ""
        focused = false
        closeSearch()
    }

    private func closeSearch() {
        guard expanded, !hidingCloseButton else { return }
        hidingCloseButton = true
    }

    @ViewBuilder
    private var icon: some View {
        let mark = Image(systemName: expanded ? "xmark" : (hasFilter ? "line.3.horizontal.decrease" : "magnifyingglass"))
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
