import AppKit
import Combine
import SeamUI
import SeamWorkspace
import SwiftUI

extension WorkspaceSession.Capture.Phase {
    var menuStatus: String {
        switch self {
        case .starting: "録音を準備中"
        case .running: "録音中"
        case .paused: "一時停止中"
        case .stopping: "録音を保存中"
        }
    }

    var menuSymbol: String {
        switch self {
        case .starting: "record.circle"
        case .running: "record.circle.fill"
        case .paused: "pause.circle.fill"
        case .stopping: "square.circle"
        }
    }
}

@MainActor
final class RecordingMenu: NSObject, NSPopoverDelegate {
    private enum Indicator: Equatable {
        case idle
        case capture(WorkspaceSession.Capture.Phase, time: String)
        case work(count: Int, running: Bool, animated: Bool)
    }

    private let service: LocalBackend
    private let popover = NSPopover()
    private var statusItem: NSStatusItem?
    private var serviceChanges: AnyCancellable?
    private var sessionChanges: AnyCancellable?
    private var displayChanges: AnyCancellable?
    private var spinner: Timer?
    private var spinnerFrame = 0
    private var indicator: Indicator?
    private var panelID: UUID?
    var openWorkspace: (() -> Void)?

    init(service: LocalBackend) {
        self.service = service
    }

    @available(macOS 26.0, *)
    func install() {
        guard statusItem == nil else { return }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem = item
        guard let button = item.button else { return }
        button.target = self
        button.action = #selector(togglePanel)
        button.imagePosition = .imageLeft

        popover.behavior = .transient
        popover.delegate = self

        serviceChanges = service.$session.sink { [weak self] session in
            self?.observe(session)
        }
        displayChanges = NSWorkspace.shared.notificationCenter
            .publisher(for: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.updateIndicator() }
    }

    private func observe(_ session: WorkspaceSession?) {
        sessionChanges = session?.objectWillChange
            .receive(on: DispatchQueue.main)
            .sink { [weak self] in self?.updateIndicator() }
        updateIndicator(session: session)
    }

    private func updateIndicator() {
        updateIndicator(session: service.session)
    }

    private func updateIndicator(session: WorkspaceSession?) {
        guard let button = statusItem?.button else { return }
        let next: Indicator
        if let capture = session?.capture {
            next = .capture(capture.phase, time: capture.phase == .starting ? "" : minutesTime(capture.elapsed))
        } else if let activities = session?.backgroundActivities, !activities.isEmpty {
            let running = activities.contains(where: \.isRunning)
            next = .work(count: activities.count, running: running,
                         animated: running && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion)
        } else {
            next = .idle
        }
        guard next != indicator else { return }
        indicator = next
        switch next {
        case .idle:
            button.image = Self.seamMark
            button.title = ""
            button.setAccessibilityLabel("Seam")
            stopSpinner()
        case let .capture(phase, time):
            button.image = NSImage(systemSymbolName: phase.menuSymbol, accessibilityDescription: nil)
            button.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .medium)
            button.title = time
            button.setAccessibilityLabel(time.isEmpty ? phase.menuStatus : "\(phase.menuStatus) \(time)")
            stopSpinner()
        case let .work(count, running, animated):
            button.title = ""
            button.setAccessibilityLabel("バックグラウンド処理 \(count)件")
            if animated {
                button.image = Self.spinnerFrames[spinnerFrame]
                startSpinner()
            } else {
                stopSpinner()
                button.image = NSImage(systemSymbolName: running ? "waveform" : "clock", accessibilityDescription: nil)
            }
        }
    }

    private func startSpinner() {
        guard spinner == nil else { return }
        let timer = Timer(timeInterval: 0.16, repeats: true) { [weak self] timer in
            MainActor.assumeIsolated {
                guard let self else {
                    timer.invalidate()
                    return
                }
                self.spinnerFrame = (self.spinnerFrame + 1) % Self.spinnerFrames.count
                self.statusItem?.button?.image = Self.spinnerFrames[self.spinnerFrame]
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        spinner = timer
    }

    private func stopSpinner() {
        spinner?.invalidate()
        spinner = nil
        spinnerFrame = 0
    }

    @available(macOS 26.0, *)
    @objc private func togglePanel() {
        if popover.isShown {
            popover.performClose(nil)
            return
        }

        guard let button = statusItem?.button else { return }
        let id = UUID()
        panelID = id
        let panel = RecordingMenuPanel(service: service) { [weak self] in
            guard let self else { return }
            self.popover.close()
            self.openWorkspace?()
        }
        .onGeometryChange(for: CGSize.self) { $0.size } action: { [weak self] size in
            // Apply the resize after SwiftUI finishes its layout pass.
            DispatchQueue.main.async {
                guard self?.panelID == id else { return }
                self?.resizePanel(to: size)
            }
        }

        let host = NSHostingController(rootView: panel)
        host.sizingOptions = []
        host.safeAreaRegions = []
        let size = host.sizeThatFits(in: CGSize(width: RecordingMenuPanel.width, height: 1_000))
        host.view.setFrameSize(size)
        popover.contentViewController = host
        popover.animates = !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        resizePanel(to: size)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        button.highlight(true)
    }

    private func resizePanel(to size: CGSize) {
        guard size.width > 0, size.height > 0 else { return }
        let rounded = CGSize(width: ceil(size.width), height: ceil(size.height))
        if popover.contentSize != rounded { popover.contentSize = rounded }
    }

    func popoverDidClose(_ notification: Notification) {
        panelID = nil
        statusItem?.button?.highlight(false)
        popover.contentViewController = nil
    }

    private static let seamMark: NSImage? = {
        let artwork = Bundle.main.url(forResource: "SeamMenuBar", withExtension: "svg")
            .flatMap(NSImage.init(contentsOf:))
        let image = artwork ?? NSImage(systemSymbolName: "waveform", accessibilityDescription: nil)
        image?.size = NSSize(width: 18, height: 18)
        image?.isTemplate = true
        return image
    }()

    private static let spinnerFrames: [NSImage] = (0..<8).map { phase in
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            for index in 0..<8 {
                let angle = Double(index) * .pi / 4
                let offset = (phase - index + 8) % 8
                NSColor.black.withAlphaComponent(1 - Double(offset) * 0.105).setFill()
                let center = CGPoint(x: 9 + cos(angle) * 6, y: 9 - sin(angle) * 6)
                NSBezierPath(ovalIn: CGRect(x: center.x - 1.15, y: center.y - 1.15,
                                           width: 2.3, height: 2.3)).fill()
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}
