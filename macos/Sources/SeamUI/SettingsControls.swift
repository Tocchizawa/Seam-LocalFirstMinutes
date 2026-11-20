import SwiftUI

@available(macOS 15.0, *)
struct SettingsRows<Content: View>: View {
    @ViewBuilder let content: () -> Content

    var body: some View {
        Group(subviews: content()) { rows in
            VStack(spacing: 0) {
                ForEach(rows) { row in
                    if row.id != rows.first?.id { settingsSeparator }
                    row
                }
            }
        }
    }
}

var settingsSeparator: some View {
    Rectangle()
        .fill(Color.primary.opacity(0.08))
        .frame(height: 1)
        .padding(.horizontal, 16)
        .accessibilityHidden(true)
}

/// 設定名と操作部品を横並びにする共通行。ページ間で見た目を揃える。
public struct SettingsRow<Control: View>: View {
    private let title: String?
    private let control: () -> Control

    public init(_ title: String? = nil, @ViewBuilder control: @escaping () -> Control) {
        self.title = title
        self.control = control
    }

    public var body: some View {
        HStack(spacing: 12) {
            if let title {
                Text(title)
                    .font(.system(size: 13))
                    .fixedSize(horizontal: false, vertical: true)
                    .layoutPriority(1)
                Spacer(minLength: 0)
            }
            control()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
    }
}

/// 設定名とトグルを並べる行。
public struct SettingsToggleRow: View {
    private let title: String
    @Binding private var isOn: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    public init(_ title: String, isOn: Binding<Bool>) {
        self.title = title
        self._isOn = isOn
    }

    public var body: some View {
        Button {
            isOn.toggle()
        } label: {
            SettingsRow(title) {
                Toggle(title, isOn: .constant(isOn))
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .allowsHitTesting(false)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(ReaderHoverStyle(tint: .primary, motion: reduceMotion ? nil : SeamMotion.feedback))
        .accessibilityRepresentation {
            Toggle(title, isOn: $isOn).toggleStyle(.switch)
        }
    }
}

@available(macOS 15.0, *)
struct SettingsDisclosure<Content: View>: View {
    private typealias Stored<Value> = SwiftUI.State<Value>
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Stored private var expanded: Bool?
    let title: String
    var initiallyExpanded = false
    @ViewBuilder let content: () -> Content

    private var showing: Bool { expanded ?? initiallyExpanded }
    private var motion: Animation? { reduceMotion ? nil : SeamMotion.reposition }

    var body: some View {
        VStack(spacing: 0) {
            Button {
                withAnimation(motion) { expanded = !showing }
            } label: {
                SettingsRow(title) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .rotationEffect(.degrees(showing ? 90 : 0))
                        .frame(width: 16)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(ReaderHoverStyle(tint: .primary, motion: motion))
            .accessibilityValue(showing ? "展開済み" : "折りたたみ")

            if showing {
                settingsSeparator
                SettingsRows(content: content)
                    .transition(reduceMotion ? .identity : .opacity)
            }
        }
    }
}

struct SettingsActionStyle: ButtonStyle {
    var tint: Color?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed
        let label = configuration.label
            .font(.system(size: 12, weight: .medium))
            .padding(.horizontal, 12)
            .frame(minWidth: 36, minHeight: 34)
            .foregroundStyle(tint ?? .primary)
            .opacity(enabled ? 1 : 0.48)
            .contentShape(Capsule())
        Group {
            if #available(macOS 26.0, *) {
                label.glassEffect(.regular.interactive(enabled), in: .capsule)
            } else {
                label.background((tint ?? .primary).opacity(pressed ? 0.14 : 0.06), in: Capsule())
            }
        }
        .scaleEffect(pressed && !reduceMotion ? 0.96 : 1)
        .animation(reduceMotion ? nil : SeamMotion.feedback, value: pressed)
    }
}
