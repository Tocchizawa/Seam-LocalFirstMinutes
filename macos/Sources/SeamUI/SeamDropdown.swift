import SwiftUI

/// インラインのボタンから開くポップオーバー。押下とホバーはツールバーと共通にする。
package struct SeamDropdown<Trigger: View, Items: View>: View {
    let name: String
    let width: CGFloat
    @ViewBuilder let trigger: () -> Trigger
    @ViewBuilder let items: () -> Items
    @State private var showing = false
    @State private var contentHeight: CGFloat = 32

    package init(_ name: String, width: CGFloat = 248,
                 @ViewBuilder trigger: @escaping () -> Trigger,
                 @ViewBuilder items: @escaping () -> Items) {
        self.name = name
        self.width = width
        self.trigger = trigger
        self.items = items
    }

    package var body: some View {
        Button { showing.toggle() } label: {
            trigger().contentShape(Rectangle())
        }
        .buttonStyle(SeamToolbarButtonStyle(active: showing))
        .accessibilityLabel(name)
        .accessibilityValue(showing ? "展開済み" : "")
        .help(name)
        .popover(isPresented: $showing, attachmentAnchor: .rect(.bounds), arrowEdge: .bottom) {
            SeamScrollView {
                rows
                    .fixedSize(horizontal: false, vertical: true)
                    .background {
                        GeometryReader { geometry in
                            Color.clear.preference(key: DropdownHeight.self, value: geometry.size.height)
                        }
                    }
            }
            .onPreferenceChange(DropdownHeight.self) { height in
                if height > 0 { contentHeight = min(height, 312) }
            }
            .frame(width: width, height: contentHeight)
            .padding(6)
            .onExitCommand { showing = false }
        }
    }

    private var rows: some View {
        VStack(alignment: .leading, spacing: 2, content: items)
    }
}

private enum DropdownHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

package struct SeamDropdownRow: View {
    let title: String
    let symbol: String?
    let selected: Bool
    let role: ButtonRole?
    let action: () -> Void
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    package init(_ title: String, symbol: String? = nil, selected: Bool = false,
                 role: ButtonRole? = nil, action: @escaping () -> Void) {
        self.title = title
        self.symbol = symbol
        self.selected = selected
        self.role = role
        self.action = action
    }

    package var body: some View {
        Button(role: role) {
            dismiss()
            action()
        } label: {
            HStack(spacing: 8) {
                if let symbol {
                    Image(systemName: symbol).frame(width: 16)
                }
                Text(title).lineLimit(2)
                Spacer(minLength: 0)
                if selected {
                    Image(systemName: "checkmark")
                        .font(.system(size: 11, weight: .semibold))
                        .accessibilityHidden(true)
                }
            }
            .font(.system(size: 13))
            .foregroundStyle(role == .destructive ? Color.red : .primary)
            .padding(.horizontal, 8)
            .frame(minHeight: 32)
            .contentShape(Rectangle())
        }
        .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

package struct SeamDropdownSelection<Value: Hashable>: View {
    let name: String
    @Binding var selection: Value
    let options: [(value: Value, label: String)]
    let unknown: String

    package init(_ name: String, selection: Binding<Value>, options: [(Value, String)], unknown: String = "") {
        self.name = name
        _selection = selection
        self.options = options
        self.unknown = unknown
    }

    package var body: some View {
        let selected = options.first { $0.value == selection }?.label ?? unknown
        SeamDropdown(name) {
            HStack(spacing: 8) {
                Text(selected).lineLimit(1).truncationMode(.middle)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
            }
            .font(.system(size: 12, weight: .medium))
            .padding(.horizontal, 12)
            .frame(minHeight: 34)
        } items: {
            ForEach(options, id: \.value) { option in
                SeamDropdownRow(option.label, selected: option.value == selection) { selection = option.value }
            }
        }
        .seamToolbarGlass()
        .frame(maxWidth: 245, alignment: .trailing)
        .accessibilityValue(selected)
    }
}
