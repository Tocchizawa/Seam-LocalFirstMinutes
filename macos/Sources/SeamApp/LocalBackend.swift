import AppKit
import SeamCore
import SeamWorkspace
import SwiftUI

@MainActor
final class LocalBackend: ObservableObject {
    enum State {
        case preparing(StartupProgress)
        case ready
        case failed(String)
    }

    enum StartupStage: Int, CaseIterable {
        case resources, environment, service

        var title: String {
            switch self {
            case .resources: "アプリの確認"
            case .environment: "セットアップ"
            case .service: "サービスの起動"
            }
        }
    }

    struct StartupProgress: Equatable {
        let stage: StartupStage
        let title: String
        var detail: String? = nil

        static let resources = Self(stage: .resources, title: "アプリのファイルを確認中")
    }

    @Published private(set) var state: State = .preparing(.resources)
    @Published private(set) var session: WorkspaceSession?
    private(set) var log: URL?
    private var task: Task<Void, Never>?
    private var sessionTask: Task<Void, Never>?
    private var process: Process?

    func startOnLaunch(port: Int?) {
        if let port {
            connect(SeamAPI(port: port))
        } else {
            start()
        }
    }

    func start() {
        guard task == nil else { return }
        state = .preparing(.resources)
        task = Task {
            defer { task = nil }
            do {
                try await run()
            } catch is CancellationError {
            } catch {
                sessionTask?.cancel()
                session = nil
                state = .failed(error.localizedDescription)
            }
        }
    }

    func stop() {
        sessionTask?.cancel()
        task?.cancel()
        if let process, process.isRunning { process.terminate() }
    }

    private func connect(_ api: SeamAPI) {
        sessionTask?.cancel()
        let session = WorkspaceSession(api: api)
        self.session = session
        sessionTask = Task { await session.run() }
        state = .ready
    }

    private struct Startup: Decodable {
        let port: Int?
        let error: String?
    }

    private struct BackendProgress: Decodable {
        let phase: String
        let message: String
    }

    private struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    private func run() async throws {
        let files = FileManager.default
        guard let resources = Bundle.main.resourceURL else {
            throw Failure(message: "録音サービスを含む Seam.app を開いてください。")
        }
        let source = resources.appendingPathComponent("backend", isDirectory: true)
        let uv = resources.appendingPathComponent("uv")
        guard files.isExecutableFile(atPath: uv.path),
              files.fileExists(atPath: source.appendingPathComponent("uv.lock").path) else {
            throw Failure(message: "録音サービスが見つかりません。同梱版の Seam.app を開いてください。")
        }

        var env = ProcessInfo.processInfo.environment
        let home = files.homeDirectoryForCurrentUser
        let data = env["SEAM_APP_DIR"].flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0) }
            ?? home.appendingPathComponent(".seam", isDirectory: true)
        let support = try files.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                    appropriateFor: nil, create: true)
            .appendingPathComponent("com.seamapp.seam.native", isDirectory: true)
        let runtime = data.appendingPathComponent("runtime", isDirectory: true)
        let logs = data.appendingPathComponent("logs", isDirectory: true)
        for directory in [support, logs] {
            try files.createDirectory(at: directory, withIntermediateDirectories: true)
        }

        let temporary = files.temporaryDirectory.appendingPathComponent("seam-" + UUID().uuidString)
        try files.createDirectory(at: temporary, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: temporary) }
        let statusFile = temporary.appendingPathComponent("status.json")
        let progressFile = temporary.appendingPathComponent("progress.json")
        let output = logs.appendingPathComponent("native-backend.log")
        log = output
        if !files.fileExists(atPath: output.path) { files.createFile(atPath: output.path, contents: nil) }
        let logHandle = try FileHandle(forWritingTo: output)
        defer { try? logHandle.close() }
        try logHandle.truncate(atOffset: 0)
        let startupLog = try? FileHandle(forReadingFrom: output)
        defer { try? startupLog?.close() }

        let standardPaths = ["/opt/homebrew/bin", "/usr/local/bin"] +
            [".local/bin", ".cargo/bin", ".bun/bin", ".npm-global/bin"].map { home.appendingPathComponent($0).path }
        let originalPath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = (standardPaths + [originalPath]).joined(separator: ":")
        env["SEAM_APP_DIR"] = data.path
        env["SEAM_DESKTOP_STATUS"] = statusFile.path
        env["SEAM_DESKTOP_PROGRESS"] = progressFile.path
        env["SEAM_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["SEAM_RESOURCES_DIR"] = resources.path
        env["SEAM_AUDIO_CAPTURE_BIN"] = resources.appendingPathComponent("audio-capture").path
        env["SEAM_SPEECH_TRANSCRIBE_BIN"] = resources.appendingPathComponent("apple-speech-transcriber").path
        env["XDG_CACHE_HOME"] = runtime.appendingPathComponent("cache").path
        env["OLLAMA_MODELS"] = runtime.appendingPathComponent("ollama/models").path
        let pythonEnvironment = support.appendingPathComponent("python-env", isDirectory: true)
        env["UV_PROJECT_ENVIRONMENT"] = pythonEnvironment.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        let child = Process()
        child.executableURL = uv
        child.currentDirectoryURL = source
        child.arguments = ["run", "--color", "never", "--frozen", "--no-dev", "--python", "3.13", "python", "-m", "src.desktop"]
        child.environment = env
        child.standardOutput = logHandle
        child.standardError = logHandle
        let hasEnvironment = files.isExecutableFile(atPath: pythonEnvironment.appendingPathComponent("bin/python").path)
        var progress = StartupProgress(
            stage: .environment,
            title: hasEnvironment ? "保存済みの実行環境を確認中" : "録音に必要なファイルを準備中",
            detail: hasEnvironment ? nil : "初回は、必要なファイルのダウンロードに時間がかかることがあります。"
        )
        state = .preparing(progress)
        try child.run()
        process = child
        defer {
            if child.isRunning { child.terminate() }
            process = nil
        }
        var api: SeamAPI?
        while child.isRunning {
            try Task.checkCancellation()
            var update = progress
            if let bytes = try? Data(contentsOf: progressFile),
               let reported = try? JSONDecoder().decode(BackendProgress.self, from: bytes) {
                let title = reported.phase == "ready" ? "録音サービスに接続中" : reported.message
                update = StartupProgress(stage: .service, title: title)
            } else if progress.stage == .environment, let startupLog,
                      let title = Self.environmentTitle(from: startupLog) {
                update = StartupProgress(stage: .environment, title: title)
            }
            if update != progress {
                progress = update
                state = .preparing(update)
            }
            if let bytes = try? Data(contentsOf: statusFile),
               let status = try? JSONDecoder().decode(Startup.self, from: bytes) {
                if let message = status.error { throw Failure(message: message) }
                if let port = status.port {
                    let candidate = api ?? SeamAPI(port: port)
                    api = candidate
                    if (try? await candidate.health(timeout: 1)) == true {
                        connect(candidate)
                        break
                    }
                }
            }
            try await Task.sleep(for: .milliseconds(400))
        }
        while child.isRunning { try await Task.sleep(for: .seconds(1)) }
        if let bytes = try? Data(contentsOf: statusFile),
           let status = try? JSONDecoder().decode(Startup.self, from: bytes),
           let message = status.error { throw Failure(message: message) }
        throw Failure(message: "録音サービスが終了しました（終了コード \(child.terminationStatus)）。")
    }

    private static func environmentTitle(from log: FileHandle) -> String? {
        // uv's console messages can add detail during setup. The backend's own
        // progress file is what advances startup to the next stage.
        guard let end = try? log.seekToEnd(), end > 0 else { return nil }
        let start = end > 8_192 ? end - 8_192 : 0
        guard (try? log.seek(toOffset: start)) != nil,
              let bytes = try? log.read(upToCount: Int(end - start)) else { return nil }
        let output = String(decoding: bytes, as: UTF8.self)
        guard let lastNewline = output.lastIndex(of: "\n") else { return nil }
        let lines = output[..<lastNewline].split(separator: "\n", omittingEmptySubsequences: false)
        for rawLine in lines.dropFirst(start == 0 ? 0 : 1).reversed() {
            let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            if line.hasPrefix("Downloading cpython-") { return "Python をダウンロード中" }
            if line.hasPrefix("Creating virtual environment") || line.hasPrefix("Downloaded cpython-") {
                return "Python の実行環境を作成中"
            }
            if line.hasPrefix("Downloading ") {
                return "必要なライブラリをダウンロード中"
            }
            if line.hasPrefix("Downloaded ") { return "必要なライブラリを準備中" }
            if line.hasPrefix("Prepared ") { return "ライブラリをインストール中" }
            if line.hasPrefix("Installed ") || line.hasPrefix("Audited ") {
                return "録音サービスを読み込み中"
            }
        }
        return nil
    }
}
