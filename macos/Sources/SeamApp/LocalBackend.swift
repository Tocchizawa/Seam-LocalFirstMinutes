import AppKit
import SeamCore
import SwiftUI

@MainActor
final class LocalBackend: ObservableObject {
    enum State {
        case preparing
        case ready(SeamAPI)
        case failed(String)
    }

    @Published private(set) var state: State = .preparing
    private(set) var log: URL?
    private var task: Task<Void, Never>?
    private var process: Process?
    private var launched = false

    func startOnLaunch() {
        guard !launched else { return }
        launched = true
        start()
    }

    func start() {
        guard task == nil else { return }
        state = .preparing
        task = Task {
            defer { task = nil }
            do {
                try await run()
            } catch is CancellationError {
            } catch {
                state = .failed(error.localizedDescription)
            }
        }
    }

    func stop() {
        task?.cancel()
        if let process, process.isRunning { process.terminate() }
    }

    private struct Startup: Decodable {
        let port: Int?
        let error: String?
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
        let hub = runtime.appendingPathComponent("hf", isDirectory: true)
        let logs = data.appendingPathComponent("logs", isDirectory: true)
        for directory in [support, hub, logs] {
            try files.createDirectory(at: directory, withIntermediateDirectories: true)
        }

        let temporary = files.temporaryDirectory.appendingPathComponent("seam-" + UUID().uuidString)
        try files.createDirectory(at: temporary, withIntermediateDirectories: false)
        defer { try? files.removeItem(at: temporary) }
        let statusFile = temporary.appendingPathComponent("status.json")
        let output = logs.appendingPathComponent("native-backend.log")
        log = output
        if !files.fileExists(atPath: output.path) { files.createFile(atPath: output.path, contents: nil) }
        let logHandle = try FileHandle(forWritingTo: output)
        defer { try? logHandle.close() }
        try logHandle.truncate(atOffset: 0)

        let standardPaths = ["/opt/homebrew/bin", "/usr/local/bin"] +
            [".local/bin", ".cargo/bin", ".bun/bin", ".npm-global/bin"].map { home.appendingPathComponent($0).path }
        let originalPath = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = (standardPaths + [originalPath]).joined(separator: ":")
        env["SEAM_APP_DIR"] = data.path
        env["SEAM_DESKTOP_STATUS"] = statusFile.path
        env["SEAM_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["SEAM_RESOURCES_DIR"] = resources.path
        env["SEAM_AUDIO_CAPTURE_BIN"] = resources.appendingPathComponent("audio-capture").path
        env["HF_HOME"] = hub.path
        env["HF_HUB_CACHE"] = hub.appendingPathComponent("hub").path
        env["HUGGINGFACE_HUB_CACHE"] = env["HF_HUB_CACHE"]
        env["XDG_CACHE_HOME"] = runtime.appendingPathComponent("cache").path
        env["OLLAMA_MODELS"] = runtime.appendingPathComponent("ollama/models").path
        env["UV_PROJECT_ENVIRONMENT"] = support.appendingPathComponent("python-env").path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        let child = Process()
        child.executableURL = uv
        child.currentDirectoryURL = source
        child.arguments = ["run", "--frozen", "--no-dev", "--python", "3.13", "python", "-m", "src.desktop"]
        child.environment = env
        child.standardOutput = logHandle
        child.standardError = logHandle
        try child.run()
        process = child
        defer {
            if child.isRunning { child.terminate() }
            process = nil
        }
        // Keep the attempt alive while uv is preparing its environment.
        var api: SeamAPI?
        while child.isRunning {
            try Task.checkCancellation()
            if let bytes = try? Data(contentsOf: statusFile),
               let status = try? JSONDecoder().decode(Startup.self, from: bytes) {
                if let message = status.error { throw Failure(message: message) }
                if let port = status.port {
                    let candidate = api ?? SeamAPI(port: port)
                    api = candidate
                    if (try? await candidate.health(timeout: 1)) == true {
                        state = .ready(candidate)
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
}

struct ServiceStartupView: View {
    @ObservedObject var service: LocalBackend

    var body: some View {
        VStack(spacing: 14) {
            switch service.state {
            case .preparing:
                ProgressView()
                Text("録音サービスを準備中").font(.headline)
                Text("初回はPythonの実行環境を準備します。").foregroundStyle(.secondary)
            case .failed(let message):
                Text("録音サービスを起動できませんでした").font(.headline)
                Text(message).foregroundStyle(.secondary).multilineTextAlignment(.center)
                HStack {
                    Button("再試行") { service.start() }
                    if let log = service.log {
                        Button("ログを開く") { NSWorkspace.shared.open(log) }
                    }
                }
            case .ready:
                EmptyView()
            }
        }
        .padding(24)
    }
}
