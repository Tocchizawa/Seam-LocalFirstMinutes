fn main() {
    ensure_audio_capture_placeholder();
    tauri_build::build()
}

fn ensure_audio_capture_placeholder() {
    let Ok(manifest_dir) = std::env::var("CARGO_MANIFEST_DIR") else {
        return;
    };
    let path = std::path::Path::new(&manifest_dir)
        .join("resources")
        .join("audio-capture");
    if path.exists() {
        return;
    }
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let content = b"#!/usr/bin/env sh\necho \"SCREEN_CAPTURE_KIT_AUDIO_ERROR audio-capture sidecar has not been built\" >&2\nexit 1\n";
    if std::fs::write(&path, content).is_ok() {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755));
        }
    }
}
