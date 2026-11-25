import Combine
import Foundation
import SeamCore
import SeamUI
import Sparkle
import SwiftUI

@MainActor
final class AppUpdates: NSObject, ObservableObject, SPUUpdaterDelegate {
    @Published private(set) var canCheck = false
    private var controller: SPUStandardUpdaterController?
    private var updaterObservers: Set<AnyCancellable> = []
    private let workInProgress: () -> Bool

    init(workInProgress: @escaping () -> Bool) {
        self.workInProgress = workInProgress
    }

    func start(migrating legacy: SettingsDocument?) {
        guard controller == nil,
              Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") is String,
              Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") is String else { return }
        migrateLegacySettings(legacy)
        let controller = SPUStandardUpdaterController(
            startingUpdater: false, updaterDelegate: self, userDriverDelegate: nil
        )
        self.controller = controller
        let updater = controller.updater
        updater.publisher(for: \.canCheckForUpdates)
            .sink { [weak self] in self?.canCheck = $0 }
            .store(in: &updaterObservers)
        updater.publisher(for: \.automaticallyChecksForUpdates, options: [.prior])
            .sink { [weak self] _ in self?.objectWillChange.send() }
            .store(in: &updaterObservers)
        updater.publisher(for: \.automaticallyDownloadsUpdates, options: [.prior])
            .sink { [weak self] _ in self?.objectWillChange.send() }
            .store(in: &updaterObservers)
        controller.startUpdater()
        if updater.automaticallyChecksForUpdates {
            updater.checkForUpdatesInBackground()
        }
    }

    private func migrateLegacySettings(_ legacy: SettingsDocument?) {
        let defaults = UserDefaults.standard
        let key = "SeamMigratedUpdateSettings"
        guard !defaults.bool(forKey: key), let legacy else { return }
        if let checks = legacy["app_update.check_on_startup"]?.flag,
           defaults.object(forKey: "SUEnableAutomaticChecks") == nil {
            defaults.set(checks, forKey: "SUEnableAutomaticChecks")
        }
        if let installs = legacy["app_update.auto_install_on_startup"]?.flag,
           defaults.object(forKey: "SUAutomaticallyUpdate") == nil {
            defaults.set(installs, forKey: "SUAutomaticallyUpdate")
        }
        defaults.set(true, forKey: key)
    }

    var settings: AppUpdateControls? {
        guard let updater = controller?.updater else { return nil }
        return AppUpdateControls(
            automaticallyChecks: Binding(
                get: { updater.automaticallyChecksForUpdates },
                set: { updater.automaticallyChecksForUpdates = $0 }
            ),
            automaticallyInstalls: Binding(
                get: { updater.automaticallyDownloadsUpdates },
                set: { updater.automaticallyDownloadsUpdates = $0 }
            ),
            canCheck: canCheck,
            check: check
        )
    }

    func check() { controller?.checkForUpdates(nil) }

    func updater(_ updater: SPUUpdater, mayPerform updateCheck: SPUUpdateCheck) throws {
        if updateCheck != .updatesInBackground && workInProgress() {
            throw NSError(domain: "com.seamapp.seam.native.update", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "録音やバックグラウンド処理が終わってからアップデートを確認してください。"
            ])
        }
    }

    func updater(_ updater: SPUUpdater, shouldPostponeRelaunchForUpdate item: SUAppcastItem,
                 untilInvokingBlock installHandler: @escaping () -> Void) -> Bool {
        guard workInProgress() else { return false }
        Task {
            while workInProgress() {
                try? await Task.sleep(for: .seconds(1))
            }
            installHandler()
        }
        return true
    }
}
