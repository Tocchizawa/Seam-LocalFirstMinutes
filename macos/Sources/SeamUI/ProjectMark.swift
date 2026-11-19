import SwiftUI

enum ProjectAccent: String, CaseIterable {
    case blue, teal, green, amber, red, pink, purple, slate

    var label: String {
        switch self {
        case .blue: "ブルー"
        case .teal: "ティール"
        case .green: "グリーン"
        case .amber: "アンバー"
        case .red: "レッド"
        case .pink: "ピンク"
        case .purple: "パープル"
        case .slate: "グレー"
        }
    }

    var tint: Color {
        switch self {
        case .blue: .blue
        case .teal: .teal
        case .green: .green
        case .amber: .orange
        case .red: .red
        case .pink: .pink
        case .purple: .purple
        case .slate: .gray
        }
    }
}

public struct ProjectMark: View {
    private let emoji: String?
    private let accent: ProjectAccent?
    private let size: CGFloat

    public init(emoji: String?, color: String?, size: CGFloat = 22) {
        self.emoji = emoji
        self.accent = color.flatMap(ProjectAccent.init(rawValue:))
        self.size = size
    }

    public var body: some View {
        Group {
            if let emoji, let symbol = emoji.first {
                Text(verbatim: String(symbol)).font(.system(size: size * 0.65))
            } else {
                Image(systemName: "folder")
                    .font(.system(size: size * 0.60, weight: .medium))
                    .foregroundStyle(accent?.tint ?? .primary)
            }
        }
        .frame(width: size, height: size)
        .background(accent.map { $0.tint.opacity(0.18) } ?? .clear, in: .rect(cornerRadius: size * 0.24))
        .accessibilityHidden(true)
    }
}
