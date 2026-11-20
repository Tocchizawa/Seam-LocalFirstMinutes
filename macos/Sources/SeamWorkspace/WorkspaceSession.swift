import AppKit
import SeamCore
import SeamUI
import SwiftUI
import UserNotifications

@MainActor
package final class AudioLevelMeter: ObservableObject {
    @Published package private(set) var samples: [Double] = []

    package var level: Double { samples.last ?? 0 }

    package func append(_ level: Double) {
        samples = Array(samples.suffix(63)) + [level]
    }

    package func reset() {
        samples = []
    }
}

@MainActor
package final class WorkspaceSession: ObservableObject {
    package struct Capture {
        package enum Phase { case starting, running, paused, stopping }

        package private(set) var id: String?
        package var phase: Phase
        package var elapsed = 0.0
        package var muted = false
        package var microphoneName: String?
        var microphoneUID: String?
        var canSwitchMicrophone = false
        var deferred = false
    }

    let api: SeamAPI
    @Published package private(set) var connected = false
    @Published package private(set) var capture: Capture?
    @Published private(set) var switchingMicrophone = false
    @Published package private(set) var isChanging = false
    @Published private(set) var transcript = LiveTranscript()
    @Published private(set) var work: [PipelineStatus] = []
    @Published private(set) var summaries: [ActiveSummary] = []
    @Published private(set) var recovery: RecoveryStatus?
    @Published var importingAudioName: String?
    @Published private(set) var archiveRevision = 0
    @Published var idleReminder: RecordingIdleReminder?
    @Published package var error: String?

    package let audioMeter = AudioLevelMeter()
    package var isBusy: Bool { isChanging || switchingMicrophone }

    package var backgroundActivities: [BackgroundActivity] {
        var activities: [BackgroundActivity] = []
        if let importingAudioName {
            activities.append(.init(id: "import", name: "音声を取り込み中",
                                    detail: importingAudioName, symbol: "waveform"))
        }
        if let recovery, recovery.state == "running" {
            let count = recovery.total > 0 ? "\(min(recovery.current, recovery.total))/\(recovery.total)件" : nil
            activities.append(.init(id: "restore", name: "録音を復元中", detail: recovery.item?.date,
                                    symbol: "arrow.trianglehead.clockwise", stage: count))
        }
        activities += work.filter { $0.sessionId != nil && $0.state != "error" }.map { $0.activity() }
        activities += summaries.map(\.activity)
        return activities.filter(\.isRunning) + activities.filter { !$0.isRunning }
    }

    private var captureRevision = 0
    private var requestedNotifications = false
    private var needsSummaryRefresh = false
    private var hasWorkSnapshot = false

    package init(api: SeamAPI) { self.api = api }

    package var canRestartForUpdate: Bool {
        hasWorkSnapshot && connected && capture == nil && backgroundActivities.isEmpty
    }

    package func legacyUpdateSettings() async -> SettingsDocument? { try? await api.settings() }

    package func run() async {
        async let snapshots: Void = poll()
        await listen()
        await snapshots
    }

    func start(in projectId: String) async {
        guard capture == nil, !isChanging else { return }
        isChanging = true
        captureRevision += 1
        capture = Capture(phase: .starting)
        defer { isChanging = false }

        do {
            let settings = try await api.settings()
            let microphone = settings["recording.mic_device"]?.number.flatMap { Int(exactly: $0) }
            let systemAudio = settings["recording.last_capture_system"]?.flag ?? true
            let result = try await api.start(projectId: projectId, micDevice: microphone, captureSystem: systemAudio)
            install(Capture(id: result.sessionId, phase: .running, microphoneName: result.micDeviceName,
                            microphoneUID: result.micDeviceUid, canSwitchMicrophone: result.micCanSwitch ?? false,
                            deferred: result.transcriptionDeferred ?? false))
            if settings["recording.stop_forget_reminder.enabled"]?.flag ?? true {
                enableBackgroundReminders()
            }
        } catch {
            // The request may have reached the service before the response was lost.
            if let status = try? await api.recordingStatus(), let current = restoredCapture(status) {
                install(current)
            } else {
                install(nil)
            }
            self.error = error.localizedDescription
        }
    }

    package func setPaused() async {
        guard let initial = capture, let id = initial.id, initial.phase != .stopping, !isChanging else { return }
        isChanging = true
        captureRevision += 1
        defer { isChanging = false }
        do {
            let paused = try await api.setPaused(initial.phase != .paused)
            if capture?.id == id { capture?.phase = paused ? .paused : .running }
        } catch { self.error = error.localizedDescription }
    }

    func toggleMicrophone() async {
        guard let current = capture, let id = current.id, current.phase != .stopping, !isChanging else { return }
        isChanging = true
        captureRevision += 1
        defer { isChanging = false }
        do {
            let muted = try await api.setMicMuted(!current.muted)
            if capture?.id == id { capture?.muted = muted }
        } catch { self.error = error.localizedDescription }
    }

    func switchMicrophone(_ deviceUID: String?) async throws {
        guard let current = capture, let id = current.id, current.phase != .stopping,
              current.canSwitchMicrophone, !switchingMicrophone, !isChanging else { return }
        switchingMicrophone = true
        defer { switchingMicrophone = false }
        let result = try await api.switchMicrophone(sessionId: id, deviceUid: deviceUID)
        guard capture?.id == id else { return }
        captureRevision += 1
        capture?.microphoneUID = result.micDeviceUid
        capture?.microphoneName = result.micDeviceName
    }

    package func stop() async {
        guard let current = capture, let id = current.id, current.phase != .stopping,
              !isChanging, !switchingMicrophone else { return }
        isChanging = true
        captureRevision += 1
        capture?.phase = .stopping
        defer { isChanging = false }
        do {
            let result = try await api.stop()
            guard capture?.id == id else { return }
            guard result.sessionId == id, result.status == "stopping" else {
                if await stopNeedsAttention(for: id) {
                    error = "録音停止後の状態を確認できませんでした。"
                }
                return
            }
        } catch {
            guard capture?.id == id else { return }
            if await stopNeedsAttention(for: id) {
                self.error = error.localizedDescription
            }
        }
    }

    /// A stop request may reach the backend even when its response is lost. Re-read
    /// server state before restoring the UI so it never claims to be recording by assumption.
    private func stopNeedsAttention(for sessionId: String) async -> Bool {
        guard capture?.id == sessionId else { return false }
        do {
            let status = try await api.recordingStatus(timeout: 4)
            guard capture?.id == sessionId else { return false }
            install(restoredCapture(status))
            return status.sessionId == sessionId && status.recording && status.stopping != true
        } catch {
            return capture?.id == sessionId
        }
    }

    func archiveChanged() {
        archiveRevision += 1
    }

    func cancelSummary(id: String) async throws {
        try await api.cancelSummary(id: id)
        await refreshSummaries()
    }

    private func poll() async {
        var cycle = 0
        while !Task.isCancelled {
            let revision = captureRevision
            do {
                let status = try await api.recordingStatus()
                try Task.checkCancellation()
                if !connected { archiveChanged() }
                connected = true
                if !isChanging && revision == captureRevision {
                    install(restoredCapture(status))
                }
                if cycle % 4 == 0 { await refreshWork() }
                else if needsSummaryRefresh { await refreshSummaries() }
                cycle += 1
            } catch {
                if Task.isCancelled { return }
                connected = false
                cycle = 0
            }
            do { try await Task.sleep(for: .seconds(1.5)) }
            catch { return }
        }
    }

    private func restoredCapture(_ status: RecordingStatus) -> Capture? {
        guard let id = status.sessionId, status.recording || status.stopping == true else { return nil }
        let phase: Capture.Phase
        if status.stopping == true { phase = .stopping }
        else { phase = status.paused == true ? .paused : .running }
        return Capture(id: id, phase: phase, elapsed: status.elapsedSec, muted: status.micMuted ?? false,
                       microphoneName: status.micDeviceName,
                       microphoneUID: status.micDeviceUid, canSwitchMicrophone: status.micCanSwitch ?? false,
                       deferred: status.transcriptionDeferred ?? false)
    }

    private func install(_ next: Capture?) {
        let oldID = transcript.sessionId
        capture = next
        guard oldID != next?.id else { return }
        idleReminder = nil
        if let oldID, Bundle.main.bundleIdentifier != nil {
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [oldID])
        }
        captureRevision += 1
        transcript.activate(next?.id)
        audioMeter.reset()
        if let id = next?.id {
            Task { await restoreTranscript(for: id) }
        }
    }

    private func restoreTranscript(for id: String) async {
        guard let saved = try? await api.segments(in: id) else { return }
        transcript.recover(saved, for: id)
    }

    private func enableBackgroundReminders() {
        guard !requestedNotifications, Bundle.main.bundleIdentifier != nil else { return }
        requestedNotifications = true
        Task {
            let center = UNUserNotificationCenter.current()
            if await center.notificationSettings().authorizationStatus == .notDetermined {
                _ = try? await center.requestAuthorization(options: [.alert, .sound])
            }
        }
    }

    private func remindWhileRecording(_ reminder: RecordingIdleReminder) {
        guard capture?.id == reminder.sessionId, capture?.phase == .running else { return }
        idleReminder = reminder
        guard !NSApp.isActive, Bundle.main.bundleIdentifier != nil else { return }
        let message = UNMutableNotificationContent()
        message.title = "録音が続いています"
        message.body = "\(reminder.silentForSec)秒間、音声入力がありません。"
        message.sound = .default
        Task {
            guard capture?.id == reminder.sessionId, capture?.phase == .running else { return }
            let request = UNNotificationRequest(identifier: reminder.sessionId, content: message, trigger: nil)
            try? await UNUserNotificationCenter.current().add(request)
        }
    }

    private func refreshWork() async {
        async let pipelines = api.pipelines()
        async let restoring = api.recoveryStatus()
        async let summaryRefresh: Void = refreshSummaries()
        if let items = try? await pipelines {
            work = items.filter(\.showsInArchive)
        }
        recovery = try? await restoring
        await summaryRefresh
        hasWorkSnapshot = true
    }

    private func refreshSummaries() async {
        needsSummaryRefresh = false
        if let active = try? await api.activeSummaries(), active != summaries { summaries = active }
    }

    private func listen() async {
        var delay = 0.5
        while !Task.isCancelled {
            do {
                let stream = try api.events()
                defer { stream.close() }
                if let id = capture?.id {
                    Task { await restoreTranscript(for: id) }
                }
                while !Task.isCancelled {
                    let event: BackendEvent
                    do { event = try await stream.next() }
                    catch is DecodingError { continue }
                    delay = 0.5
                    accept(event)
                }
            } catch {
                do { try await Task.sleep(for: .seconds(delay)) }
                catch { return }
                delay = min(5, delay * 2)
            }
        }
    }

    private func accept(_ event: BackendEvent) {
        switch event {
        case .recording(let update):
            guard capture?.id == update.sessionId else { return }
            capture?.elapsed = update.elapsedSec
            if capture?.phase != .stopping, let paused = update.paused {
                capture?.phase = paused ? .paused : .running
            }
            if let deferred = update.transcriptionDeferred { capture?.deferred = deferred }
        case .stopped(let result):
            guard let id = result.sessionId, id == capture?.id else { return }
            install(nil)
            if let problem = result.error { error = problem }
        case .recordingIdle(let reminder):
            remindWhileRecording(reminder)
        case .level(let reading):
            guard capture?.id == reading.sessionId else { return }
            audioMeter.append(reading.combinedLevel)
        case .segment(let row):
            transcript.receive(row)
        case .draft(let draft):
            transcript.receive(draft)
        case .transcriptionError(let failure):
            error = failure.message
        case .pipelineError(let failure):
            if let id = failure.sessionId, capture?.id == id { install(nil) }
            error = failure.message
        case .pipeline(let progress):
            guard let id = progress.sessionId else { return }
            if let index = work.firstIndex(where: { $0.sessionId == id }) {
                if progress.showsInArchive { work[index] = progress }
                else { work.remove(at: index) }
            } else if progress.showsInArchive {
                work.insert(progress, at: 0)
            }
            if progress.state == "transcribing", capture?.id == id, capture?.phase == .stopping {
                install(nil)
            } else if progress.state == "error", capture?.id == id {
                install(nil)
                if let message = progress.error { error = message }
            }
        case .other(let type):
            if ["summary_stage", "summary_done", "summary_failed", "summary_cancelled", "summary_skipped",
                "dictionary_update_started", "dictionary_update_stage", "dictionary_update_finished"].contains(type) {
                needsSummaryRefresh = true
            }
            if ["minutes_saved", "minutes_title_updated", "summary_done", "summary_failed", "pipeline_done",
                "session_recovery_finished", "transcript_corrected"].contains(type) {
                archiveChanged()
            }
        }
    }
}

extension PipelineStatus {
    func activity(title: String? = nil) -> BackgroundActivity {
        let failed = state == "error"
        let name = failed ? "文字起こしに失敗" : (state == "stopping" ? "録音を保存中" : "文字起こし中")
        let detail = title ?? minutesTitle ?? startedAt.flatMap(formattedRecordingDate)
        return .init(id: "transcribe-\(sessionId ?? "")", name: name, detail: detail,
                     symbol: failed ? "exclamationmark.circle" : "waveform",
                     progress: progress, stage: failed ? nil : stageLabel ?? message, isRunning: !failed)
    }
}

func summaryActivity(id: String, state: String?, stage: String?, label: String?, title: String? = nil) -> BackgroundActivity {
    let waiting = state == "queued" || stage == "dictionary_waiting"
    let (name, symbol) = switch state {
    case "queued": ("要約を待機中", "clock")
    case "dictionary_update": (waiting ? "用語辞書の更新を待機中" : "用語辞書を更新中", "text.book.closed")
    default: ("要約中", "doc.text")
    }
    return .init(id: "summary-\(id)", name: name, detail: title, symbol: symbol,
                 stage: waiting ? nil : label, isRunning: !waiting)
}

extension ActiveSummary {
    var activity: BackgroundActivity {
        summaryActivity(id: minutesId, state: state, stage: stage, label: stageLabel, title: title)
    }
}
