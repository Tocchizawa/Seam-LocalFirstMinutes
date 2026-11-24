import AppKit
import SeamCore
import SwiftUI

public struct MeetingNoteBadge: View {
    public let note: MeetingNote

    public init(note: MeetingNote) { self.note = note }

    public var body: some View {
        Image(systemName: "tag.fill")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(Color.accentColor)
            .help("\(note.title)\n\(note.body)")
            .accessibilityLabel("\(note.title)、\(note.body)")
    }
}

public struct MeetingNoteActions: View {
    public let note: MeetingNote?
    public let edit: () -> Void
    public let remove: () -> Void

    public init(note: MeetingNote?, edit: @escaping () -> Void, remove: @escaping () -> Void) {
        self.note = note
        self.edit = edit
        self.remove = remove
    }

    public var body: some View {
        if note != nil {
            Button("メモを編集…", systemImage: "pencil", action: edit)
            Button("メモを削除", systemImage: "trash", role: .destructive, action: remove)
        } else {
            Button("メモを追加…", systemImage: "tag", action: edit)
        }
    }
}

package extension View {
    func transcriptNoteMenu(text: String, note: MeetingNote? = nil, draft: Bool = false,
                            edit: (() -> Void)? = nil, remove: (() -> Void)? = nil, askChat: (() -> Void)? = nil) -> some View {
        modifier(TranscriptNoteMenu(text: text, note: note, draft: draft, edit: edit, remove: remove, askChat: askChat))
    }
}

private struct TranscriptNoteMenu: ViewModifier {
    @Environment(\.isEnabled) private var enabled
    let text: String
    let note: MeetingNote?
    let draft: Bool
    let edit: (() -> Void)?
    let remove: (() -> Void)?
    let askChat: (() -> Void)?

    private var items: [TranscriptMenuItem] {
        var items = [
            TranscriptMenuItem("発言をコピー", symbol: "document.on.document") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            },
        ]
        if let askChat {
            items.append(TranscriptMenuItem("チャットで聞く", symbol: "bubble.left", action: askChat))
        }
        if draft {
            items.append(TranscriptMenuItem("発言が確定するとメモを追加できます", symbol: "tag"))
        } else if note != nil {
            items.append(TranscriptMenuItem("メモを編集…", symbol: "pencil", action: edit))
            items.append(TranscriptMenuItem("メモを削除", symbol: "trash", destructive: true, action: remove))
        } else if let edit {
            items.append(TranscriptMenuItem("メモを追加…", symbol: "tag", action: edit))
        }
        return items
    }

    func body(content: Content) -> some View {
        content
            .contextMenu {
                ForEach(items) { item in
                    Button(item.title, systemImage: item.symbol, role: item.destructive ? .destructive : nil) {
                        item.action?()
                    }
                    .disabled(item.action == nil)
                }
            }
            .background {
                TranscriptMenuRegion(items: items, enabled: enabled)
                    .accessibilityHidden(true)
            }
    }
}

private struct TranscriptMenuItem: Identifiable {
    let title: String
    let symbol: String
    let destructive: Bool
    let action: (() -> Void)?
    var id: String { title }

    init(_ title: String, symbol: String, destructive: Bool = false, action: (() -> Void)? = nil) {
        self.title = title
        self.symbol = symbol
        self.destructive = destructive
        self.action = action
    }
}

/// macOSの選択可能なTextは独自のメニューを先に開くため、ウィンドウ内で対象行の副クリックだけを受け取る。
private struct TranscriptMenuRegion: NSViewRepresentable {
    let items: [TranscriptMenuItem]
    let enabled: Bool

    func makeNSView(context: Context) -> Region { Region(frame: .zero) }
    func updateNSView(_ view: Region, context: Context) {
        view.items = items
        view.enabled = enabled
    }

    final class Region: NSView {
        var items: [TranscriptMenuItem] = []
        var enabled = true
        private var monitor: Any?

        override func hitTest(_ point: NSPoint) -> NSView? { nil }

        override func viewWillMove(toWindow newWindow: NSWindow?) {
            if let monitor {
                NSEvent.removeMonitor(monitor)
                self.monitor = nil
            }
            super.viewWillMove(toWindow: newWindow)
            if newWindow != nil {
                monitor = NSEvent.addLocalMonitorForEvents(matching: [.rightMouseDown, .leftMouseDown]) { [weak self] event in
                    self?.handleMenuEvent(event) == true ? nil : event
                }
            }
        }

        private func handleMenuEvent(_ event: NSEvent) -> Bool {
            guard enabled, let window, event.window === window, window.attachedSheet == nil,
                  event.type == .rightMouseDown || event.modifierFlags.contains(.control),
                  visibleRect.contains(convert(event.locationInWindow, from: nil)) else { return false }
            let menu = NSMenu()
            menu.autoenablesItems = false
            for (index, item) in items.enumerated() {
                let entry = NSMenuItem(title: item.title, action: #selector(selectItem(_:)), keyEquivalent: "")
                entry.target = self
                entry.tag = index
                entry.image = NSImage(systemSymbolName: item.symbol, accessibilityDescription: nil)
                entry.isEnabled = item.action != nil
                menu.addItem(entry)
            }
            NSMenu.popUpContextMenu(menu, with: event, for: self)
            return true
        }

        @objc private func selectItem(_ sender: NSMenuItem) {
            if items.indices.contains(sender.tag) { items[sender.tag].action?() }
        }
    }
}
