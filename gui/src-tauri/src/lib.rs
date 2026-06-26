use serde::Serialize;
use std::fs;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{include_image, AppHandle, Emitter, Manager, RunEvent, WindowEvent};

/// Splash 画面に渡す起動進捗。
#[derive(Serialize, Clone, Debug)]
struct BackendStatus {
    /// "starting" | "python" | "venv" | "resolve" | "download" | "build" | "install" | "ready" | "error"
    phase: String,
    /// 表示用テキスト (例: "依存ライブラリをダウンロード中: torch")
    message: String,
    /// 0.0-1.0。None なら不確定(スピナー)
    progress: Option<f32>,
    /// 追加詳細(uv の生ログ等。必要に応じてフロントで表示)
    detail: Option<String>,
}

#[derive(Serialize, Clone, Debug, Default)]
struct StartupSnapshot {
    status: Option<BackendStatus>,
    logs: Vec<String>,
}

struct StartupState(Mutex<StartupSnapshot>);

#[derive(Clone, Debug)]
struct StartupFailure {
    message: String,
    detail: Option<String>,
}

impl StartupFailure {
    fn new(message: impl Into<String>, detail: Option<String>) -> Self {
        Self {
            message: message.into(),
            detail,
        }
    }

    fn status(&self) -> BackendStatus {
        BackendStatus {
            phase: "error".into(),
            message: self.message.clone(),
            progress: None,
            detail: self.detail.clone(),
        }
    }
}

const DEFAULT_OLLAMA_HOST: &str = "127.0.0.1:11434";
const STARTUP_LOG_CAP: usize = 80;

struct ManagedProcessState {
    backend: Option<Child>,
    ollama: Option<Child>,
}

struct ManagedProcesses(Mutex<ManagedProcessState>);

impl ManagedProcesses {
    fn kill(&self) {
        let mut guard = self.0.lock().unwrap();
        kill_child(&mut guard.backend, "backend");
        kill_child(&mut guard.ollama, "ollama");
    }
}

fn kill_managed_processes<R: tauri::Runtime>(app: &tauri::AppHandle<R>, reason: &str) {
    if let Some(state) = app.try_state::<ManagedProcesses>() {
        eprintln!("[app] process cleanup on {}", reason);
        state.kill();
    }
}

fn kill_child(child_slot: &mut Option<Child>, label: &str) {
    if let Some(mut child) = child_slot.take() {
        let pid = child.id() as i32;
        eprintln!("[{}] Stopping process group: {}", label, pid);

        #[cfg(unix)]
        unsafe {
            libc::kill(-pid, libc::SIGTERM);
        }

        std::thread::sleep(Duration::from_millis(500));

        if let Ok(None) = child.try_wait() {
            #[cfg(unix)]
            unsafe {
                libc::kill(-pid, libc::SIGKILL);
            }
            #[cfg(not(unix))]
            {
                let _ = child.kill();
            }
        }
        let _ = child.wait();
        eprintln!("[{}] Stopped", label);
    }
}

#[cfg(unix)]
fn is_process_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    unsafe {
        if libc::kill(pid, 0) == 0 {
            return true;
        }
    }
    std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

fn command_for_pid(pid: i32) -> Option<String> {
    let output = Command::new("ps")
        .args(["-o", "command=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let cmd = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if cmd.is_empty() {
        None
    } else {
        Some(cmd)
    }
}

fn is_stale_seam_backend_command(cmd: &str) -> bool {
    cmd.contains("python -m src.main") || cmd.contains("python3 -m src.main")
}

#[cfg(unix)]
fn kill_process_group_by_pid(pid: i32, reason: &str) {
    if pid <= 1 {
        return;
    }
    eprintln!(
        "[backend] stopping stale process group pid={} ({})",
        pid, reason
    );
    unsafe {
        libc::kill(-pid, libc::SIGTERM);
    }
    std::thread::sleep(Duration::from_millis(500));
    if is_process_alive(pid) {
        unsafe {
            libc::kill(-pid, libc::SIGKILL);
        }
    }
}

#[cfg(unix)]
fn kill_stale_backend_listener(port: u16) -> Vec<String> {
    let mut conflicts = Vec::new();
    let target = format!("TCP:{}", port);
    let output = match Command::new("lsof")
        .args(["-nP", "-i", &target, "-sTCP:LISTEN", "-t"])
        .output()
    {
        Ok(o) => o,
        Err(e) => {
            eprintln!("[backend] lsof failed while checking stale listener: {}", e);
            return conflicts;
        }
    };
    if !output.status.success() {
        return conflicts;
    }

    let self_pid = std::process::id() as i32;
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let Ok(pid) = trimmed.parse::<i32>() else {
            continue;
        };
        if pid <= 1 || pid == self_pid {
            continue;
        }
        let cmd = command_for_pid(pid).unwrap_or_default();
        if !is_stale_seam_backend_command(&cmd) {
            let detail = format!("port {} is occupied by pid={} cmd={}", port, pid, cmd);
            eprintln!("[backend] {}", detail);
            conflicts.push(detail);
            continue;
        }
        kill_process_group_by_pid(pid, "pre-start cleanup");
    }
    conflicts
}

#[cfg(not(unix))]
fn kill_stale_backend_listener(_port: u16) -> Vec<String> {
    Vec::new()
}

fn app_dir() -> PathBuf {
    match std::env::var("HOME") {
        Ok(home) if !home.is_empty() => Path::new(&home).join(".seam"),
        _ => PathBuf::from(".seam"),
    }
}

fn runtime_dir() -> PathBuf {
    app_dir().join("runtime")
}

fn bundled_resources_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    Some(exe.parent()?.parent()?.join("Resources"))
}

/// Finder/Dock から起動した .app は launchd の最小 PATH
/// (`/usr/bin:/bin:/usr/sbin:/sbin`) しか持たず、Homebrew や cargo 等で
/// 入れた `codex` / `claude` 等の CLI を解決できない。
/// よく使われるインストール先を先頭に足した PATH を返し、backend に渡す。
fn augmented_path() -> String {
    let home = std::env::var("HOME").unwrap_or_default();
    let mut prefixes: Vec<String> = vec![
        "/opt/homebrew/bin".into(),
        "/opt/homebrew/sbin".into(),
        "/usr/local/bin".into(),
    ];
    if !home.is_empty() {
        prefixes.push(format!("{home}/.local/bin"));
        prefixes.push(format!("{home}/.cargo/bin"));
        prefixes.push(format!("{home}/.bun/bin"));
        prefixes.push(format!("{home}/.deno/bin"));
        prefixes.push(format!("{home}/.npm-global/bin"));
    }

    let existing = std::env::var("PATH").unwrap_or_default();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut out: Vec<String> = Vec::new();
    for p in prefixes
        .into_iter()
        .chain(existing.split(':').map(|s| s.to_string()))
        .chain(
            ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
                .iter()
                .map(|s| s.to_string()),
        )
    {
        if p.is_empty() || !seen.insert(p.clone()) {
            continue;
        }
        out.push(p);
    }
    out.join(":")
}

fn ensure_runtime_dirs() -> PathBuf {
    let runtime = runtime_dir();
    let dirs = [
        runtime.clone(),
        runtime.join("hf"),
        runtime.join("hf").join("hub"),
        runtime.join("hf").join("transformers"),
        runtime.join("cache"),
        runtime.join("ollama"),
        runtime.join("ollama").join("models"),
    ];
    for d in dirs {
        if let Err(e) = fs::create_dir_all(&d) {
            eprintln!("[runtime] Failed to create {}: {}", d.display(), e);
        }
    }
    runtime
}

fn spawn_with_own_group(mut cmd: Command) -> Result<Child, String> {
    #[cfg(unix)]
    unsafe {
        use std::os::unix::process::CommandExt;
        cmd.pre_exec(|| {
            libc::setpgid(0, 0);
            Ok(())
        });
    }
    match cmd.spawn() {
        Ok(c) => Ok(c),
        Err(e) => {
            eprintln!("[proc] spawn failed: {}", e);
            Err(e.to_string())
        }
    }
}

/// バックエンドの実行に必要なソース/uv の所在。
/// 配布 .app では Contents/Resources/ から展開し ~/Library/Application Support/.../python-env に置く。
/// 開発機ではリポジトリのルート + 既存 uv を使う。
struct BackendLayout {
    work_dir: PathBuf,
    uv_path: String,
}

fn remove_path_if_exists(path: &Path) -> std::io::Result<()> {
    if path.is_dir() {
        fs::remove_dir_all(path)
    } else if path.exists() {
        fs::remove_file(path)
    } else {
        Ok(())
    }
}

fn restore_backend_backup(work_dir: &Path, backup_dir: &Path) -> bool {
    if !backup_dir.exists() {
        return true;
    }
    for name in ["src", "pyproject.toml", "uv.lock"] {
        let target = work_dir.join(name);
        if let Err(e) = remove_path_if_exists(&target) {
            eprintln!("[backend] cleanup partial {} failed: {}", name, e);
            return false;
        }
        let backup = backup_dir.join(name);
        if backup.exists() {
            if let Err(e) = fs::rename(&backup, &target) {
                eprintln!("[backend] restore previous {} failed: {}", name, e);
                return false;
            }
        }
    }
    if let Err(e) = remove_path_if_exists(backup_dir) {
        eprintln!("[backend] cleanup restored backup failed: {}", e);
        return false;
    }
    true
}

fn replace_bundled_backend(work_dir: &Path, bundle_tar: &Path) -> bool {
    let temp_dir = work_dir.join(".extracting-backend");
    let backup_dir = work_dir.join(".previous-backend");
    if !restore_backend_backup(work_dir, &backup_dir) {
        return false;
    }
    if let Err(e) = remove_path_if_exists(&temp_dir) {
        eprintln!("[backend] cleanup temp extract failed: {}", e);
        return false;
    }
    if let Err(e) = fs::create_dir_all(&temp_dir) {
        eprintln!("[backend] temp mkdir failed: {}", e);
        return false;
    }

    let status = Command::new("/usr/bin/tar")
        .arg("-xzf")
        .arg(bundle_tar)
        .arg("-C")
        .arg(&temp_dir)
        .status();
    match status {
        Ok(s) if s.success() => {}
        Ok(s) => {
            eprintln!("[backend] tar exited: {:?}", s.code());
            let _ = remove_path_if_exists(&temp_dir);
            return false;
        }
        Err(e) => {
            eprintln!("[backend] tar failed: {}", e);
            let _ = remove_path_if_exists(&temp_dir);
            return false;
        }
    }

    for name in ["src", "pyproject.toml", "uv.lock"] {
        if !temp_dir.join(name).exists() {
            eprintln!("[backend] extracted backend missing {}", name);
            let _ = remove_path_if_exists(&temp_dir);
            return false;
        }
    }

    if let Err(e) = fs::create_dir_all(&backup_dir) {
        eprintln!("[backend] backup mkdir failed: {}", e);
        let _ = remove_path_if_exists(&temp_dir);
        return false;
    }

    for name in ["src", "pyproject.toml", "uv.lock"] {
        let target = work_dir.join(name);
        if target.exists() {
            if let Err(e) = fs::rename(&target, backup_dir.join(name)) {
                eprintln!("[backend] backup old {} failed: {}", name, e);
                let _ = restore_backend_backup(work_dir, &backup_dir);
                let _ = remove_path_if_exists(&temp_dir);
                return false;
            }
        }
    }

    for name in ["src", "pyproject.toml", "uv.lock"] {
        let from = temp_dir.join(name);
        let to = work_dir.join(name);
        if let Err(e) = fs::rename(&from, &to) {
            eprintln!("[backend] install {} failed: {}", name, e);
            if !restore_backend_backup(work_dir, &backup_dir) {
                eprintln!("[backend] previous backend restore is incomplete");
            }
            let _ = remove_path_if_exists(&temp_dir);
            return false;
        }
    }

    let _ = remove_path_if_exists(&temp_dir);
    if let Err(e) = remove_path_if_exists(&backup_dir) {
        eprintln!("[backend] cleanup backup failed after install: {}", e);
        return false;
    }
    true
}

fn detect_bundled_layout() -> Option<BackendLayout> {
    // exe: .app/Contents/MacOS/Seam → resources: .app/Contents/Resources/
    let resources = bundled_resources_dir()?;
    let bundled_uv = resources.join("uv");
    let bundle_tar = resources.join("seam-backend.tar.gz");
    if !bundled_uv.exists() || !bundle_tar.exists() {
        return None;
    }
    let home = std::env::var("HOME").ok()?;
    let work_dir =
        PathBuf::from(home).join("Library/Application Support/com.seamapp.seam/python-env");

    // 再展開判定: 同梱 tarball の mtime + size を marker に焼き込み、
    // 値が変わった場合のみ再展開する。version 文字列だけだと "0.1.0" のまま
    // src/ を更新したケースで取りこぼすため。
    let marker = work_dir.join(".extracted-signature");
    let tar_meta = fs::metadata(&bundle_tar).ok()?;
    let tar_size = tar_meta.len();
    let tar_mtime = tar_meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let current_sig = format!("{}:{}:{}", env!("CARGO_PKG_VERSION"), tar_size, tar_mtime);

    let stale = match fs::read_to_string(&marker) {
        Ok(v) => v.trim() != current_sig,
        Err(_) => true,
    };
    if stale {
        eprintln!(
            "[backend] extracting bundled backend → {}",
            work_dir.display()
        );
        if let Err(e) = fs::create_dir_all(&work_dir) {
            eprintln!("[backend] mkdir failed: {}", e);
            return None;
        }
        if !replace_bundled_backend(&work_dir, &bundle_tar) {
            return None;
        }
        let _ = fs::write(&marker, &current_sig);
    }

    Some(BackendLayout {
        work_dir,
        uv_path: bundled_uv.display().to_string(),
    })
}

fn dev_layout() -> Option<BackendLayout> {
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    let project_root = Path::new(manifest_dir).parent().and_then(|p| p.parent())?;
    let uv_path = which_uv()?;
    Some(BackendLayout {
        work_dir: project_root.to_path_buf(),
        uv_path,
    })
}

fn start_backend(runtime_root: &Path) -> Result<Child, StartupFailure> {
    let app_root = app_dir();
    let whisper_hf_home = runtime_root.join("hf");
    let whisper_hub_cache = whisper_hf_home.join("hub");
    let whisper_tf_cache = whisper_hf_home.join("transformers");
    let xdg_cache = runtime_root.join("cache");

    let app_root_s = app_root.display().to_string();
    let parent_pid_s = std::process::id().to_string();
    let hf_home_s = whisper_hf_home.display().to_string();
    let hf_hub_cache_s = whisper_hub_cache.display().to_string();
    let tf_cache_s = whisper_tf_cache.display().to_string();
    let xdg_cache_s = xdg_cache.display().to_string();
    let ollama_models_s = runtime_root
        .join("ollama")
        .join("models")
        .display()
        .to_string();
    let ollama_base_s = format!("http://{}", DEFAULT_OLLAMA_HOST);

    // 配布 .app 同梱モードを優先、無ければ dev モード (リポジトリ + 既存 uv)
    let layout = detect_bundled_layout().or_else(dev_layout);
    let layout = match layout {
        Some(l) => l,
        None => {
            let detail = "bundled resources missing, and uv not found".to_string();
            eprintln!("[backend] No backend layout available ({})", detail);
            return Err(StartupFailure::new(
                "バックエンドの実行環境を検出できませんでした",
                Some(detail),
            ));
        }
    };
    println!("[backend] work_dir = {}", layout.work_dir.display());
    println!("[backend] uv       = {}", layout.uv_path);

    let mut cmd = Command::new(&layout.uv_path);
    cmd.args(["run", "python", "-m", "src.main"]);

    if let Some(resources) = bundled_resources_dir() {
        cmd.env("SEAM_RESOURCES_DIR", &resources);
        let audio_capture = resources.join("audio-capture");
        if audio_capture.exists() {
            cmd.env("SEAM_AUDIO_CAPTURE_BIN", audio_capture);
        }
    }

    // uv の venv キャッシュも user data dir 配下へ (bundled モード)。
    // dev モードでは標準の .venv をリポジトリ直下に置く既存挙動を維持。
    let is_bundled = !layout.work_dir.ends_with("議事録ん")
        && layout.work_dir.to_string_lossy().contains("python-env");
    if is_bundled {
        let venv = layout.work_dir.join(".venv");
        cmd.env("UV_PROJECT_ENVIRONMENT", venv.display().to_string());
    }

    cmd.current_dir(&layout.work_dir)
        .env("SEAM_APP_DIR", &app_root_s)
        .env("SEAM_PARENT_PID", &parent_pid_s)
        .env("SEAM_RUNTIME_DIR", runtime_root.display().to_string())
        // Finder/Dock 起動時の最小 PATH を補い、Homebrew/cargo 等で入れた
        // codex/claude CLI を backend が解決できるようにする
        .env("PATH", augmented_path())
        .env("HF_HOME", &hf_home_s)
        .env("HF_HUB_CACHE", &hf_hub_cache_s)
        .env("HUGGINGFACE_HUB_CACHE", &hf_hub_cache_s)
        .env("TRANSFORMERS_CACHE", &tf_cache_s)
        .env("XDG_CACHE_HOME", &xdg_cache_s)
        .env("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
        .env("OLLAMA_MODELS", &ollama_models_s)
        .env("OLLAMA_BASE_URL", &ollama_base_s)
        // tqdm/print のバッファリングを切ってリアルタイムに stderr が流れるように
        .env("PYTHONUNBUFFERED", "1")
        // HuggingFace の DL 進捗バーを必ず stderr に出す (非 TTY 時の自動 disable を抑止)
        .env("HF_HUB_DISABLE_PROGRESS_BARS", "0")
        .env("TQDM_MININTERVAL", "0.3")
        // stderr は \n / \r で分割して splash に流すため piped。
        // HuggingFace の tqdm は \r で上書きするのでそれも 1 行として拾う。
        .stdout(Stdio::inherit())
        .stderr(Stdio::piped());

    let child = spawn_with_own_group(cmd)
        .map_err(|e| StartupFailure::new("バックエンドプロセスを起動できませんでした", Some(e)))?;
    println!("[backend] Started Python backend (PID: {})", child.id());
    Ok(child)
}

fn start_backend_with_retries(
    runtime_root: &Path,
    attempts: usize,
) -> Result<Child, StartupFailure> {
    let attempts = attempts.max(1);
    let mut last_failure: Option<StartupFailure> = None;
    for attempt in 1..=attempts {
        match start_backend(runtime_root) {
            Ok(child) => return Ok(child),
            Err(failure) => {
                eprintln!(
                    "[backend] start attempt {}/{} failed: {}",
                    attempt, attempts, failure.message
                );
                if let Some(detail) = &failure.detail {
                    eprintln!("[backend] start failure detail: {}", detail);
                }
                last_failure = Some(failure);
            }
        }
        if attempt < attempts {
            std::thread::sleep(Duration::from_millis(700));
        }
    }
    Err(last_failure.unwrap_or_else(|| {
        StartupFailure::new(
            "バックエンドの起動に失敗しました",
            Some(format!(
                "backend process could not be started after {} attempts",
                attempts
            )),
        )
    }))
}

fn which_uv() -> Option<String> {
    let home = std::env::var("HOME").unwrap_or_default();
    let candidates = [
        "/opt/homebrew/bin/uv".to_string(),
        "/usr/local/bin/uv".to_string(),
        format!("{}/.local/bin/uv", home),
        format!("{}/.cargo/bin/uv", home),
    ];

    for path in &candidates {
        if std::path::Path::new(path).exists() {
            return Some(path.clone());
        }
    }

    Command::new("which").arg("uv").output().ok().and_then(|o| {
        if o.status.success() {
            String::from_utf8(o.stdout)
                .ok()
                .map(|s| s.trim().to_string())
        } else {
            None
        }
    })
}

/// ANSI エスケープシーケンス (\x1b[...m など) を除去。
/// char 単位で処理することで UTF-8 マルチバイト (日本語等) を壊さない。
fn strip_ansi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            // CSI シーケンス: ESC '[' ... letter
            if chars.peek() == Some(&'[') {
                chars.next();
                while let Some(&peek) = chars.peek() {
                    chars.next();
                    if peek.is_ascii_alphabetic() {
                        break;
                    }
                }
            }
            // ESC で始まる他の制御 (OSC 等) はとりあえず ESC のみ捨てる
            continue;
        }
        out.push(c);
    }
    out
}

/// uv / uvicorn / Python (seam-progress) の stderr 1行から進捗情報を抽出する。
/// マッチしない行は None。
fn parse_startup_line(raw: &str) -> Option<BackendStatus> {
    let line = strip_ansi(raw);
    let l = line.trim();
    if l.is_empty() {
        return None;
    }

    // 最優先: Python が emit する [seam-progress] {json} 行
    if let Some(rest) = l.strip_prefix("[seam-progress] ") {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(rest) {
            let phase = v
                .get("phase")
                .and_then(|x| x.as_str())
                .unwrap_or("starting")
                .to_string();
            let message = v
                .get("message")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string();
            let progress = v.get("progress").and_then(|x| x.as_f64()).map(|f| f as f32);
            let detail = v
                .get("detail")
                .and_then(|x| x.as_str())
                .map(|s| s.to_string());
            return Some(BackendStatus {
                phase,
                message,
                progress,
                detail,
            });
        }
    }

    if l.contains("Uvicorn running") || l.contains("Application startup complete") {
        return Some(BackendStatus {
            phase: "ready".into(),
            message: "起動完了".into(),
            progress: Some(1.0),
            detail: None,
        });
    }
    if l.starts_with("Using CPython") || l.starts_with("Using Python") {
        return Some(BackendStatus {
            phase: "python".into(),
            message: "Python ランタイムを準備中".into(),
            progress: Some(0.05),
            detail: Some(l.to_string()),
        });
    }
    if l.starts_with("Downloading CPython") || l.contains("Installing CPython") {
        return Some(BackendStatus {
            phase: "python".into(),
            message: "Python ランタイムをダウンロード中".into(),
            progress: Some(0.08),
            detail: Some(l.to_string()),
        });
    }
    if l.starts_with("Creating virtual environment") || l.starts_with("Created virtual environment")
    {
        return Some(BackendStatus {
            phase: "venv".into(),
            message: "仮想環境を作成中".into(),
            progress: Some(0.12),
            detail: None,
        });
    }
    if l.starts_with("Resolved ") {
        return Some(BackendStatus {
            phase: "resolve".into(),
            message: "依存ライブラリを解決中".into(),
            progress: Some(0.18),
            detail: Some(l.to_string()),
        });
    }
    if let Some(rest) = l.strip_prefix("Downloading ") {
        // "Downloading torch (2.1GB)" など
        return Some(BackendStatus {
            phase: "download".into(),
            message: format!("ダウンロード中: {}", rest),
            progress: Some(0.30),
            detail: None,
        });
    }
    if l.starts_with("Built ") || l.starts_with("Building ") {
        return Some(BackendStatus {
            phase: "build".into(),
            message: "ライブラリをビルド中".into(),
            progress: Some(0.55),
            detail: Some(l.to_string()),
        });
    }
    if l.starts_with("Prepared ") {
        return Some(BackendStatus {
            phase: "install".into(),
            message: "インストール準備中".into(),
            progress: Some(0.75),
            detail: Some(l.to_string()),
        });
    }
    if l.starts_with("Installed ") {
        return Some(BackendStatus {
            phase: "install".into(),
            message: "インストール完了".into(),
            progress: Some(0.90),
            detail: Some(l.to_string()),
        });
    }
    // uvicorn の typical line。startup hook がここから走り Python 側が
    // [seam-progress] でより細かい進捗を出すので、ここでは 0.70 までに留める。
    if l.contains("Started server process") || l.contains("Waiting for application") {
        return Some(BackendStatus {
            phase: "starting".into(),
            message: "サーバ起動中".into(),
            progress: Some(0.70),
            detail: None,
        });
    }
    None
}

fn remember_startup_status<R: tauri::Runtime>(app: &AppHandle<R>, status: &BackendStatus) {
    if let Some(state) = app.try_state::<StartupState>() {
        let mut guard = state.0.lock().unwrap();
        guard.status = Some(status.clone());
    }
}

fn emit_backend_log<R: tauri::Runtime>(app: &AppHandle<R>, line: String) {
    if let Some(state) = app.try_state::<StartupState>() {
        let mut guard = state.0.lock().unwrap();
        guard.logs.push(line.clone());
        if guard.logs.len() > STARTUP_LOG_CAP {
            let overflow = guard.logs.len() - STARTUP_LOG_CAP;
            guard.logs.drain(0..overflow);
        }
    }
    let _ = app.emit("backend-log", line);
}

fn emit_status<R: tauri::Runtime>(app: &AppHandle<R>, status: BackendStatus) {
    remember_startup_status(app, &status);
    eprintln!("[backend-status] {:?}", status);
    let _ = app.emit("backend-status", status);
}

fn emit_status_with_replay<R: tauri::Runtime>(app: &AppHandle<R>, status: BackendStatus) {
    emit_status(app, status.clone());
    for delay_ms in [500_u64, 1500] {
        let app = app.clone();
        let status = status.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(delay_ms));
            emit_status(&app, status);
        });
    }
}

fn emit_startup_failure<R: tauri::Runtime>(app: &AppHandle<R>, failure: &StartupFailure) {
    emit_status_with_replay(app, failure.status());
    emit_backend_log(app, failure.message.clone());
    if let Some(detail) = &failure.detail {
        emit_backend_log(app, detail.clone());
    }
}

fn exit_status_detail(status: ExitStatus) -> String {
    match status.code() {
        Some(code) => format!("backend exited with code {}", code),
        None => "backend exited by signal".into(),
    }
}

/// stderr を line-by-line で読みつつ、進捗イベントを emit する。
/// 読み取った行はそのまま parent stderr にも出力 (デバッグ用)。
/// stderr を byte 単位で読み、\n / \r どちらでも 1 行として分割する。
/// HuggingFace の tqdm 進捗バーは \r で上書きするため、\n だけだと
/// ダウンロード完了まで何も出てこない。\r も区切りとして拾うと
/// リアルタイムに進捗ラインがログに流れる。
fn spawn_stderr_reader<R: tauri::Runtime>(app: AppHandle<R>, stderr: std::process::ChildStderr) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stderr);
        let mut buf: Vec<u8> = Vec::with_capacity(4096);
        let mut chunk = [0u8; 4096];

        let flush = |app: &AppHandle<R>, buf: &mut Vec<u8>| {
            if buf.is_empty() {
                return;
            }
            // UTF-8 として解釈 (失敗時は lossy)
            let raw = String::from_utf8_lossy(buf).to_string();
            buf.clear();

            // 既知パターンから進捗を抽出
            if let Some(status) = parse_startup_line(&raw) {
                emit_status(app, status);
            }

            // 生ログを Splash へ (ANSI 除去のみ)。[seam-progress] は内部信号なので非表示。
            let sanitized = strip_ansi(&raw);
            let trimmed = sanitized.trim_end();
            if !trimmed.is_empty() && !trimmed.trim_start().starts_with("[seam-progress] ") {
                eprintln!("[backend-stderr] {}", trimmed);
                emit_backend_log(app, trimmed.to_string());
            }
        };

        loop {
            match reader.read(&mut chunk) {
                Ok(0) => {
                    flush(&app, &mut buf);
                    break;
                }
                Ok(n) => {
                    for &b in &chunk[..n] {
                        if b == b'\n' || b == b'\r' {
                            flush(&app, &mut buf);
                        } else {
                            buf.push(b);
                        }
                    }
                }
                Err(_) => {
                    flush(&app, &mut buf);
                    break;
                }
            }
        }
    });
}

fn spawn_backend_exit_monitor<R: tauri::Runtime>(app: AppHandle<R>) {
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(500));
        let Some(state) = app.try_state::<ManagedProcesses>() else {
            return;
        };
        let mut guard = state.0.lock().unwrap();
        let Some(child) = guard.backend.as_mut() else {
            return;
        };
        match child.try_wait() {
            Ok(None) => continue,
            Ok(Some(status)) => {
                let detail = exit_status_detail(status);
                guard.backend = None;
                drop(guard);
                emit_startup_failure(
                    &app,
                    &StartupFailure::new("バックエンドが起動前に終了しました", Some(detail)),
                );
                return;
            }
            Err(e) => {
                guard.backend = None;
                drop(guard);
                emit_startup_failure(
                    &app,
                    &StartupFailure::new(
                        "バックエンドの状態確認に失敗しました",
                        Some(e.to_string()),
                    ),
                );
                return;
            }
        }
    });
}

fn show_main_window<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[tauri::command]
fn restart_backend(app: AppHandle) -> Result<(), String> {
    emit_status(
        &app,
        BackendStatus {
            phase: "starting".into(),
            message: "バックエンドを再起動中...".into(),
            progress: Some(0.02),
            detail: None,
        },
    );

    {
        let Some(state) = app.try_state::<ManagedProcesses>() else {
            return Err("managed process state is unavailable".into());
        };
        let mut guard = state.0.lock().unwrap();
        kill_child(&mut guard.backend, "backend");
    }

    let port_conflicts = kill_stale_backend_listener(18900);
    if !port_conflicts.is_empty() {
        let detail = port_conflicts.join("\n");
        emit_startup_failure(
            &app,
            &StartupFailure::new(
                "バックエンドの再起動に失敗しました: port 18900 が使用中です",
                Some(detail.clone()),
            ),
        );
        return Err(detail);
    }

    let runtime_root = ensure_runtime_dirs();
    let mut backend_child = match start_backend_with_retries(&runtime_root, 3) {
        Ok(child) => Some(child),
        Err(failure) => {
            let detail = match &failure.detail {
                Some(detail) => format!("{}\n{}", failure.message, detail),
                None => failure.message.clone(),
            };
            emit_startup_failure(
                &app,
                &StartupFailure::new("バックエンドの再起動に失敗しました", Some(detail.clone())),
            );
            return Err(detail);
        }
    };
    let backend_stderr = backend_child.as_mut().and_then(|c| c.stderr.take());

    emit_status(
        &app,
        BackendStatus {
            phase: "starting".into(),
            message: "バックエンドを起動中...".into(),
            progress: Some(0.02),
            detail: None,
        },
    );
    {
        let Some(state) = app.try_state::<ManagedProcesses>() else {
            return Err("managed process state is unavailable".into());
        };
        let mut guard = state.0.lock().unwrap();
        guard.backend = backend_child;
    }
    if let Some(stderr) = backend_stderr {
        spawn_stderr_reader(app.clone(), stderr);
    }
    spawn_backend_exit_monitor(app);
    Ok(())
}

#[tauri::command]
fn backend_process_status(app: AppHandle) -> BackendStatus {
    let Some(state) = app.try_state::<ManagedProcesses>() else {
        return BackendStatus {
            phase: "error".into(),
            message: "バックエンド状態を取得できません".into(),
            progress: None,
            detail: Some("managed process state is unavailable".into()),
        };
    };

    let mut guard = state.0.lock().unwrap();
    let wait_result = match guard.backend.as_mut() {
        Some(child) => child.try_wait(),
        None => {
            return BackendStatus {
                phase: "error".into(),
                message: "バックエンドが起動していません".into(),
                progress: None,
                detail: Some("backend process is not running".into()),
            };
        }
    };

    match wait_result {
        Ok(Some(status)) => {
            let detail = exit_status_detail(status);
            guard.backend.take();
            BackendStatus {
                phase: "error".into(),
                message: "バックエンドが停止しました".into(),
                progress: None,
                detail: Some(detail),
            }
        }
        Ok(None) => BackendStatus {
            phase: "starting".into(),
            message: "バックエンドを起動中...".into(),
            progress: Some(0.02),
            detail: None,
        },
        Err(e) => BackendStatus {
            phase: "error".into(),
            message: "バックエンド状態を取得できません".into(),
            progress: None,
            detail: Some(e.to_string()),
        },
    }
}

#[tauri::command]
fn get_startup_snapshot(state: tauri::State<'_, StartupState>) -> StartupSnapshot {
    state.0.lock().unwrap().clone()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let runtime_root = ensure_runtime_dirs();
    // 旧プロセスが孤立して 18900 を掴んだままだと、新しい起動が既存プロセスに
    // 吸われてモデル状態が壊れたままになる。起動前に stale backend を掃除する。
    let port_conflicts = kill_stale_backend_listener(18900);
    let ollama_child = None;
    let backend_result = if port_conflicts.is_empty() {
        start_backend_with_retries(&runtime_root, 3)
    } else {
        Err(StartupFailure::new(
            "バックエンドを起動できません: port 18900 が使用中です",
            Some(port_conflicts.join("\n")),
        ))
    };
    let (mut backend_child, backend_start_failure) = match backend_result {
        Ok(child) => (Some(child), None),
        Err(failure) => (None, Some(failure)),
    };
    // 同梱バックエンドの stderr を取り出して後で reader thread に渡す。
    // (start_backend の Cmd::stderr(piped) のおかげで Some になっているはず)
    let backend_stderr = backend_child.as_mut().and_then(|c| c.stderr.take());

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .invoke_handler(tauri::generate_handler![
            get_startup_snapshot,
            restart_backend,
            backend_process_status
        ])
        .setup(move |app| {
            app.manage(StartupState(Mutex::new(StartupSnapshot::default())));
            app.manage(ManagedProcesses(Mutex::new(ManagedProcessState {
                backend: backend_child,
                ollama: ollama_child,
            })));

            let app_handle = app.handle().clone();
            if let Some(failure) = &backend_start_failure {
                emit_startup_failure(&app_handle, failure);
            } else {
                emit_status(
                    &app_handle,
                    BackendStatus {
                        phase: "starting".into(),
                        message: "バックエンドを起動中...".into(),
                        progress: Some(0.02),
                        detail: None,
                    },
                );
                spawn_backend_exit_monitor(app_handle.clone());
            }
            if let Some(stderr) = backend_stderr {
                spawn_stderr_reader(app_handle, stderr);
            }

            // ─── Tray icon (macOS menu bar) ─────────────────────
            let show_item = MenuItem::with_id(app, "show", "ウィンドウを表示", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let quit_item = MenuItem::with_id(app, "quit", "Seam を終了", true, None::<&str>)?;

            let tray_menu = Menu::with_items(app, &[&show_item, &separator, &quit_item])?;

            // メニューバー用専用アイコン (黒+透過、template image)。
            // 通常の app icon は白squircle背景なので template モードで真っ白になる。
            let tray_icon = include_image!("./icons/tray-icon.png");

            let _tray = TrayIconBuilder::with_id("main-tray")
                .icon(tray_icon)
                .icon_as_template(true) // monochrome template (macOS)
                .tooltip("Seam")
                .menu(&tray_menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        show_main_window(app);
                    }
                    "quit" => {
                        // 終了操作時に先に子プロセス停止を実行してからアプリ終了。
                        kill_managed_processes(app, "TrayQuit");
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        show_main_window(app);
                    }
                })
                .build(app)?;

            // ─── Window close (左上 ×) → hide window のみ ───────
            // プロセス終了は Dock/Tray の「終了」または ExitRequested 系でのみ行う。
            if let Some(window) = app.get_webview_window("main") {
                let window_handle = window.clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        // 左上 × はアプリ終了ではなく最小化/常駐扱いにする。
                        if let Err(e) = window_handle.hide() {
                            eprintln!("[window] hide failed on CloseRequested: {}", e);
                        }
                    }
                });
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            match event {
                #[cfg(target_os = "macos")]
                RunEvent::Reopen { .. } => {
                    // Dock アイコン再クリック時に隠したメインウィンドウを復帰。
                    show_main_window(app);
                }
                RunEvent::ExitRequested { .. } => {
                    // Exit より前の段階でも必ず子プロセス停止を試みる。
                    kill_managed_processes(app, "ExitRequested");
                }
                RunEvent::Exit => {
                    // 最終段階でも念のため停止を再実行(冪等)。
                    kill_managed_processes(app, "Exit");
                }
                _ => {}
            }
        });
}
