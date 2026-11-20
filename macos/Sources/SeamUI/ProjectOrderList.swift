import SwiftUI

/// プロジェクト行の並べ替え。ドラッグ中は隣の行が空いた位置へ移動する。
/// 行の高さは init の height、行間は spacing で調整する。
public struct ProjectOrderList<Row: View, MenuItems: View>: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private typealias Stored<Value> = SwiftUI.State<Value>

    @Binding private var ids: [String]
    @Stored private var picked: String?
    @Stored private var translation: CGFloat = 0
    @Stored private var destination: Int?
    private let row: (String) -> Row
    private let menuItems: (String) -> MenuItems
    private let height: CGFloat
    private let animated: Bool
    private let spacing: CGFloat = 3

    public init(ids: Binding<[String]>, height: CGFloat = 33, animated: Bool = true,
                @ViewBuilder row: @escaping (String) -> Row,
                @ViewBuilder menuItems: @escaping (String) -> MenuItems) {
        _ids = ids
        self.height = height
        self.animated = animated
        self.row = row
        self.menuItems = menuItems
    }

    public var body: some View {
        VStack(spacing: spacing) {
            ForEach(ids, id: \.self) { id in
                row(id)
                    .frame(height: height)
                    .contentShape(Rectangle())
                    .background {
                        if picked == id {
                            RoundedRectangle(cornerRadius: 8)
                                .fill(.regularMaterial)
                                .shadow(color: .black.opacity(0.12), radius: 7, y: 3)
                        }
                    }
                    .offset(y: offset(for: id))
                    .zIndex(picked == id ? 1 : 0)
                    .highPriorityGesture(
                        DragGesture(minimumDistance: 5, coordinateSpace: .global)
                            .onChanged { drag(id, distance: $0.translation.height) }
                            .onEnded { _ in finish(id) }
                    )
                    .contextMenu {
                        menuItems(id)
                        Button("上に移動") { move(id, by: -1) }.disabled(ids.first == id)
                        Button("下に移動") { move(id, by: 1) }.disabled(ids.last == id)
                    }
                    .accessibilityAction(named: Text("上に移動")) { move(id, by: -1) }
                    .accessibilityAction(named: Text("下に移動")) { move(id, by: 1) }
                    .help("行をドラッグして並べ替え")
            }
        }
        .onDisappear { release() }
    }

    private var step: CGFloat { height + spacing }
    private var slide: Animation? { reduceMotion || !animated ? nil : SeamMotion.reposition }

    private func drag(_ id: String, distance: CGFloat) {
        guard let source = ids.firstIndex(of: id), picked == nil || picked == id, ids.count > 1 else { return }
        let last = ids.count - 1
        let lower = -CGFloat(source) * step
        let upper = CGFloat(last - source) * step
        let normalized = min(upper, max(lower, distance))
        withAnimation(nil) {
            picked = id
            translation = normalized
        }

        let proposed = min(last, max(0, source + Int((normalized / step).rounded())))
        if destination != proposed {
            withAnimation(slide) { destination = proposed }
        }
    }

    private func finish(_ id: String) {
        guard picked == id else { return }
        withAnimation(slide) {
            if let source = ids.firstIndex(of: id), let destination, source != destination {
                var reordered = ids
                reordered.remove(at: source)
                reordered.insert(id, at: min(destination, reordered.count))
                ids = reordered
            }
            release()
        }
    }

    private func offset(for id: String) -> CGFloat {
        guard let picked, let from = ids.firstIndex(of: picked), let index = ids.firstIndex(of: id) else { return 0 }
        if id == picked { return translation }
        guard let to = destination else { return 0 }
        if index > from && index <= to { return -step }
        if index < from && index >= to { return step }
        return 0
    }

    private func move(_ id: String, by amount: Int) {
        guard let index = ids.firstIndex(of: id), ids.indices.contains(index + amount) else { return }
        var reordered = ids
        reordered.swapAt(index, index + amount)
        withAnimation(slide) { ids = reordered }
    }

    private func release() {
        picked = nil
        destination = nil
        translation = 0
    }
}

public extension ProjectOrderList where MenuItems == EmptyView {
    init(ids: Binding<[String]>, height: CGFloat = 33, animated: Bool = true,
         @ViewBuilder row: @escaping (String) -> Row) {
        self.init(ids: ids, height: height, animated: animated, row: row) { _ in EmptyView() }
    }
}
