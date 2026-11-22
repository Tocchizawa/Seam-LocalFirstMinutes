import AppKit
import Combine
import SeamUI
import SeamWorkspace
import SwiftUI

@main
struct SeamApp: App {
    @NSApplicationDelegateAdaptor(SeamApplicationDelegate.self) private var delegate

    var body: some Scene {
        Window("Seam", id: "workspace") {
            if #available(macOS 26.0, *) {
                SeamWindow(service: delegate.service, updates: delegate.updates, recordingMenu: delegate.recordingMenu)
            } else {
                Text("macOS 26以降が必要です").padding(40)
            }
        }
        .defaultSize(width: 1040, height: 660)
        .windowToolbarStyle(.unified(showsTitle: false))
        .windowResizability(.contentMinSize)
        .commands {
            UpdateCommands(updates: delegate.updates)
        }
    }
}

@MainActor
final class SeamApplicationDelegate: NSObject, NSApplicationDelegate {
    let service = LocalBackend()
    lazy var recordingMenu = RecordingMenu(service: service)
    lazy var updates = AppUpdates { [weak service] in service?.session?.canRestartForUpdate != true }
    private var backendReady: AnyCancellable?

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard #available(macOS 26.0, *) else { return }
        recordingMenu.install()
        let arguments = ProcessInfo.processInfo.arguments
        let port = arguments.drop(while: { $0 != "--port" }).dropFirst().first
            .flatMap(Int.init).flatMap { (1...65535).contains($0) ? $0 : nil }
        backendReady = service.$session.compactMap { $0 }.first().sink { [weak self] session in
            Task { self?.updates.start(migrating: await session.legacyUpdateSettings()) }
        }
        service.startOnLaunch(port: port)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        if !hasVisibleWindows {
            recordingMenu.openWorkspace?()
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        service.stop()
    }
}

private struct UpdateCommands: Commands {
    @ObservedObject var updates: AppUpdates

    var body: some Commands {
        CommandGroup(after: .appInfo) {
            if updates.settings != nil {
                Button("アップデートを確認…", action: updates.check)
                    .disabled(!updates.canCheck)
            }
        }
    }
}

@available(macOS 26.0, *)
private struct SeamWindow: View {
    @ObservedObject var service: LocalBackend
    @ObservedObject var updates: AppUpdates
    let recordingMenu: RecordingMenu
    @Environment(\.openWindow) private var openWindow
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
                    .zIndex(1)
            }
        }
        .onAppear {
            let open = openWindow
            recordingMenu.openWorkspace = {
                NSApp.activate(ignoringOtherApps: true)
                open(id: "workspace")
            }
        }
        .environment(\.appUpdateControls, updates.settings)
        .tint(.primary)
        .animation(reduceMotion ? nil : SeamMotion.update, value: service.session != nil)
        .preferredColorScheme(appearance == "light" ? .light : (appearance == "dark" ? .dark : nil))
        .frame(minWidth: 770, minHeight: 550)
        .toolbar {
            // 起動後は Workspace のボタンがこの位置に入る。
            if service.session == nil {
                ToolbarItem(placement: .navigation) {
                    Color.clear.frame(width: 36, height: 36)
                        .accessibilityHidden(true)
                }
                .sharedBackgroundVisibility(.hidden)
            }
        }
        .toolbarBackgroundVisibility(.hidden, for: .windowToolbar)
    }
}
