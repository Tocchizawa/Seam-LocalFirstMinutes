import AppKit
import SeamUI
import SeamWorkspace
import SwiftUI

@main
struct SeamApp: App {
    @NSApplicationDelegateAdaptor(SeamApplicationDelegate.self) private var delegate

    var body: some Scene {
        Window("Seam", id: "workspace") {
            if #available(macOS 26.0, *) {
                SeamWindow(service: delegate.service)
            } else {
                Text("macOS 26以降が必要です").padding(40)
            }
        }
        .defaultSize(width: 1040, height: 660)
        .windowToolbarStyle(.unified(showsTitle: false))
        .windowResizability(.contentMinSize)

        if #available(macOS 26.0, *) {
            MenuBarExtra {
                RecordingMenuPanel(service: delegate.service)
            } label: {
                RecordingMenuBarLabel(service: delegate.service)
            }
            .menuBarExtraStyle(.window)
        }
    }
}

@MainActor
final class SeamApplicationDelegate: NSObject, NSApplicationDelegate {
    let service = LocalBackend()

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard #available(macOS 26.0, *) else { return }
        let arguments = ProcessInfo.processInfo.arguments
        let port = arguments.drop(while: { $0 != "--port" }).dropFirst().first
            .flatMap(Int.init).flatMap { (1...65535).contains($0) ? $0 : nil }
        service.startOnLaunch(port: port)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        if !hasVisibleWindows,
           let window = NSApp.windows.first(where: { $0.title == "Seam" }) {
            window.makeKeyAndOrderFront(nil)
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        service.stop()
    }
}

@available(macOS 26.0, *)
private struct SeamWindow: View {
    @ObservedObject var service: LocalBackend
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @AppStorage("appearance") private var appearance = "system"

    var body: some View {
        ZStack {
            if let session = service.session {
                Workspace(session: session)
                    .id(ObjectIdentifier(session))
                    .transition(.opacity)
            } else {
                ServiceStartupView(service: service)
                    .transition(.opacity)
            }
        }
        .animation(reduceMotion ? nil : SeamMotion.update, value: service.session != nil)
        .preferredColorScheme(appearance == "light" ? .light : (appearance == "dark" ? .dark : nil))
        .frame(minWidth: 770, minHeight: 550)
        .toolbar {
            // ウィンドウが表示されている間は、起動の状態に関わらず同じ標準ツールバーを使う。
            ToolbarItem(placement: .navigation) {
                Color.clear.frame(width: 1, height: 36)
                    .accessibilityHidden(true)
            }
            .sharedBackgroundVisibility(.hidden)
        }
        .toolbarBackgroundVisibility(.hidden, for: .windowToolbar)
    }
}
