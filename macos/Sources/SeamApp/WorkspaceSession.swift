import AppKit
import SeamCore
import SwiftUI
import UserNotifications

@MainActor
final class WorkspaceSession: ObservableObject {
    struct Capture {
        enum Phase { case starting, running, paused, stopping }

        var id: String?
        var phase: Phase
        var elapsed = 0.0
        var muted = false
        var deferred = false
    }

    let api: SeamAPI
    @Published private(set) var connected = false
    @Published private(set) var capture: Capture?
    @Published private(set) var transcript = LiveTranscript()
    @Published private(set) var levels: [Double] = []
    @Published private(set) var work: [PipelineStatus] = []
    @Published private(set) var summaries: [ActiveSummary] = []
    @Published private(set) var recovery: RecoveryStatus?
    @Published private(set) var archiveRevision = 0
    @Published var idleReminder: RecordingIdleReminder?
    @Published var error: String?

    private var changing = false
    private var captureRevision = 0
    private var requestedNotifications = false
    private var needsSummaryRefresh = false

    init(api: SeamAPI) { self.api = api }

    func run() async {
        async let snapshots: Void = poll()
        await listen()
        await snapshots
    }

    func start(in projectId: String) async {
        guard capture == nil, !changing else { return }
        changing = true
        captureRevision += 1
        capture = Capture(phase: .starting)
        defer { changing = false }

        do {
            let settings = try await api.settings()
            let microphone = settings["recording.mic_device"]?.number.flatMap { Int(exactly: $0) }
            let systemAudio = settings["recording.last_capture_system"]?.flag ?? true
            let result = try await api.start(projectId: projectId, micDevice: microphone, captureSystem: systemAudio)
            install(Capture(id: result.sessionId, phase: .running, deferred: result.transcriptionDeferred ?? false))
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

    func setPaused() async {
        guard let initial = capture, let id = initial.id, initial.phase != .stopping, !changing else { return }
        changing = true
        captureRevision += 1
        defer { changing = false }
        do {
            let paused = try await api.setPaused(initial.phase != .paused)
            if capture?.id == id { capture?.phase = paused ? .paused : .running }
        } catch { self.error = error.localizedDescription }
    }

    func toggleMicrophone() async {
        guard let current = capture, let id = current.id, current.phase != .stopping, !changing else { return }
        changing = true
        captureRevision += 1
        defer { changing = false }
        do {
            let muted = try await api.setMicMuted(!current.muted)
            if capture?.id == id { capture?.muted = muted }
        } catch { self.error = error.localizedDescription }
    }

    func stop() async {
        guard let current = capture, let id = current.id, current.phase != .stopping, !changing else { return }
        changing = true
        captureRevision += 1
        capture?.phase = .stopping
        defer { changing = false }
        do {
            _ = try await api.stop()
        } catch {
            if capture?.id == id { capture?.phase = current.phase }
            self.error = error.localizedDescription
        }
    }

    func archiveChanged() {
        archiveRevision += 1
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
                if !changing && revision == captureRevision {
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
        levels = []
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
            levels = Array(levels.suffix(63)) + [reading.combinedLevel]
        case .segment(let row):
            transcript.receive(row)
        case .draft(let draft):
            transcript.receive(draft)
        case .pipeline(let progress):
            guard let id = progress.sessionId else { return }
            work.removeAll { $0.sessionId == id }
            if progress.showsInArchive { work.insert(progress, at: 0) }
            if progress.state == "transcribing", capture?.id == id, capture?.phase == .stopping {
                install(nil)
            }
        case .other(let type):
            if ["summary_stage", "summary_done", "summary_failed", "summary_cancelled", "summary_skipped",
                "dictionary_update_started", "dictionary_update_finished"].contains(type) {
                needsSummaryRefresh = true
            }
            if ["minutes_saved", "minutes_title_updated", "summary_done", "summary_failed", "pipeline_done", "session_recovery_finished"].contains(type) {
                archiveChanged()
            }
        }
    }
}
