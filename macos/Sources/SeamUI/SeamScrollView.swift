import AppKit
import SwiftUI

/// スクロールバーを内容に重ねて表示し、表示切り替えで本文の幅が変わらないようにする。
public struct SeamScrollView<Content: View>: View {
    private let axes: Axis.Set
    private let content: () -> Content

    public init(_ axes: Axis.Set = .vertical, @ViewBuilder content: @escaping () -> Content) {
        self.axes = axes
        self.content = content
    }

    public var body: some View {
        ScrollView(axes) {
            content()
                .background(alignment: .topLeading) {
                    ScrollIndicatorRegistration(editor: false)
                        .frame(width: 0, height: 0)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
        }
    }
}

public extension View {
    func overlayEditorScrollIndicators() -> some View {
        background {
            ScrollIndicatorRegistration(editor: true)
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }
}

private struct ScrollIndicatorRegistration: NSViewRepresentable {
    let editor: Bool

    func makeNSView(context: Context) -> ScrollIndicatorView { ScrollIndicatorView(editor: editor) }
    func updateNSView(_ view: ScrollIndicatorView, context: Context) { view.applyStyle() }
}

private final class ScrollIndicatorView: NSView {
    private let editor: Bool

    init(editor: Bool) {
        self.editor = editor
        super.init(frame: .zero)
        NotificationCenter.default.addObserver(self, selector: #selector(preferredStyleChanged),
                                               name: NSScroller.preferredScrollerStyleDidChangeNotification, object: nil)
    }

    required init?(coder: NSCoder) { nil }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        applyStyle()
        if editor, window != nil {
            DispatchQueue.main.async { [weak self] in self?.applyStyle() }
        }
    }

    override func layout() {
        super.layout()
        if editor { applyStyle() }
    }

    func applyStyle() {
        guard window != nil, let scrollView = editor ? textEditorScrollView() : enclosingScrollView,
              scrollView.scrollerStyle != .overlay else { return }
        scrollView.scrollerStyle = .overlay
    }

    @objc private func preferredStyleChanged() {
        // AppKit applies the new system preference before the app restores its overlay style.
        DispatchQueue.main.async { [weak self] in self?.applyStyle() }
    }

    private func textEditorScrollView() -> NSScrollView? {
        let frameInWindow = convert(bounds, to: nil)
        guard !frameInWindow.isEmpty else { return nil }

        func findEditor(in view: NSView) -> NSScrollView? {
            if let scrollView = view as? NSScrollView {
                let frame = scrollView.convert(scrollView.bounds, to: nil)
                if frame.insetBy(dx: -1, dy: -1).contains(frameInWindow),
                   frameInWindow.insetBy(dx: -1, dy: -1).contains(frame) {
                    return scrollView
                }
            }
            return view.subviews.lazy.compactMap(findEditor).first
        }

        // Look alongside the probe, stopping at the editor's containing scroll region.
        var branch: NSView = self
        while let container = branch.superview, !(container is NSScrollView) {
            for sibling in container.subviews where sibling !== branch {
                if let found = findEditor(in: sibling) { return found }
            }
            branch = container
        }
        return nil
    }
}
