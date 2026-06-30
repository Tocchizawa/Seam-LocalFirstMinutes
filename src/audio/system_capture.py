"""Core Audio Process Tap sidecar でシステム内部音声をキャプチャする。"""
from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import threading
import time
from pathlib import Path

from src.config import config

logger = logging.getLogger(__name__)

CORE_AUDIO_TAP_MIN_VERSION = (14, 2)


def _find_ffmpeg() -> str:
    import shutil

    # 配布ビルドは imageio_ffmpeg 同梱の静的 binary を優先
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass

    path = shutil.which("ffmpeg")
    if path:
        return path
    for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if Path(p).exists():
            return p
    return "ffmpeg"


def _normalize_capture_method(value: object | None = None) -> str:
    raw = value
    if raw is None:
        raw = config.get("recording", "system_capture", default="auto")
    method = str(raw or "auto").strip().lower().replace("-", "_")
    aliases = {
        "coreaudio": "coreaudio_tap",
        "core_audio": "coreaudio_tap",
        "core_audio_tap": "coreaudio_tap",
        "process_tap": "coreaudio_tap",
        "tap": "coreaudio_tap",
        # 旧設定値。ScreenCaptureKit フォールバックは廃止したため Core Audio Tap 扱いに寄せる。
        "sck": "auto",
        "screen_capture_kit": "auto",
        "screen_capturekit": "auto",
        "screencapturekit": "auto",
        "blackhole": "auto",
    }
    return aliases.get(method, method if method else "auto")


def _macos_version_tuple() -> tuple[int, int]:
    version = platform.mac_ver()[0]
    parts: list[int] = []
    for part in version.split(".")[:2]:
        try:
            parts.append(int(part))
        except Exception:
            parts.append(0)
    while len(parts) < 2:
        parts.append(0)
    return parts[0], parts[1]


def _core_audio_tap_os_supported() -> bool:
    if platform.system() != "Darwin":
        return False
    return _macos_version_tuple() >= CORE_AUDIO_TAP_MIN_VERSION


def _find_audio_capture_sidecar() -> Path | None:
    env_path = os.environ.get("SEAM_AUDIO_CAPTURE_BIN")
    if env_path:
        path = Path(env_path)
        if path.exists() and os.access(path, os.X_OK) and not _is_placeholder_sidecar(path):
            return path

    resources_dir = os.environ.get("SEAM_RESOURCES_DIR")
    candidates: list[Path] = []
    if resources_dir:
        candidates.extend([
            Path(resources_dir) / "audio-capture",
            Path(resources_dir) / "sidecar" / "audio-capture",
        ])

    repo_root = Path(__file__).resolve().parents[2]
    candidates.extend([
        repo_root / "sidecar" / "audio-capture" / ".build" / "release" / "audio-capture",
        repo_root / "sidecar" / "audio-capture" / ".build" / "debug" / "audio-capture",
        repo_root / "gui" / "src-tauri" / "resources" / "audio-capture",
    ])
    for path in candidates:
        if path.exists() and os.access(path, os.X_OK) and not _is_placeholder_sidecar(path):
            return path
    return None


def _is_placeholder_sidecar(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(300).decode("utf-8", errors="ignore")
        return "sidecar has not been built" in head
    except Exception:
        return False


def _core_audio_tap_available() -> bool:
    return _core_audio_tap_os_supported() and _find_audio_capture_sidecar() is not None


def is_available() -> bool:
    method = _normalize_capture_method()
    if method == "coreaudio_tap":
        return _core_audio_tap_available()
    return _core_audio_tap_available()


class SystemAudioCapture:
    def __init__(self) -> None:
        self._running = False
        self._wav_file = None
        self._raw_path: Path | None = None
        self._wav_path: Path | None = None
        self._meta_path: Path | None = None
        self._error: str | None = None
        self._sample_rate: int = 48000
        self._backend: str | None = None
        self._process: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: list[str] = []
        self._tail_thread: threading.Thread | None = None
        self._tail_stop = threading.Event()
        self._raw_format = "f32le"
        self._external_callback = None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def backend(self) -> str | None:
        return self._backend

    def start(self, output_path: Path, sample_rate: int = 48000,
              external_callback=None) -> None:
        if self._running:
            raise RuntimeError("Already capturing")

        self._error = None
        self._backend = None
        self._external_callback = external_callback

        method = _normalize_capture_method()
        if method in {"auto", "coreaudio_tap"}:
            try:
                self._start_core_audio_tap(output_path)
                return
            except Exception as e:
                self._error = f"Core Audio Tap のキャプチャ開始に失敗: {e}"
                raise RuntimeError(self._error)

        self._error = f"未対応の内部音声キャプチャ方式です: {method}"
        raise RuntimeError(self._error)

    def _start_core_audio_tap(self, output_path: Path) -> None:
        if not _core_audio_tap_os_supported():
            raise RuntimeError("Core Audio Tap は macOS 14.2+ でのみ利用できます")

        sidecar = _find_audio_capture_sidecar()
        if sidecar is None:
            raise RuntimeError("Core Audio Tap sidecar が見つかりません")

        self._backend = "coreaudio_tap"
        self._raw_path = output_path.with_suffix(".raw")
        self._wav_path = output_path
        self._meta_path = output_path.with_suffix(".meta.json")
        self._raw_format = "f32le"
        self._stderr_lines = []
        self._tail_stop.clear()

        for path in (self._raw_path, self._meta_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

        cmd = [
            str(sidecar),
            str(self._raw_path),
            "--meta-path",
            str(self._meta_path),
        ]
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            self._process = None
            raise RuntimeError(f"Core Audio Tap sidecar の起動に失敗: {e}") from e

        self._stderr_thread = threading.Thread(
            target=self._drain_sidecar_stderr,
            daemon=True,
            name="coreaudio-tap-stderr",
        )
        self._stderr_thread.start()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._meta_path.exists():
                try:
                    meta = json.loads(self._meta_path.read_text())
                    raw_format = str(meta.get("format") or "f32le").lower()
                    if raw_format != "f32le":
                        raise RuntimeError(f"unsupported sidecar format: {raw_format}")
                    self._raw_format = raw_format
                    self._sample_rate = max(8000, min(192000, int(float(meta.get("sample_rate") or 48000))))
                except Exception as e:
                    self._stop_sidecar_process()
                    raise RuntimeError(f"Core Audio Tap metadata の読み込みに失敗: {e}") from e

                if self._external_callback is not None:
                    self._tail_thread = threading.Thread(
                        target=self._tail_raw_audio,
                        daemon=True,
                        name="coreaudio-tap-raw-tail",
                    )
                    self._tail_thread.start()

                self._running = True
                logger.info(
                    "System audio capture started (Core Audio Tap %dHz → %s)",
                    self._sample_rate,
                    output_path,
                )
                return

            code = self._process.poll() if self._process is not None else None
            if code is not None:
                err = self._sidecar_error_tail()
                self._stop_sidecar_process()
                raise RuntimeError(f"Core Audio Tap sidecar exited early ({code}): {err}")
            time.sleep(0.05)

        self._stop_sidecar_process()
        raise RuntimeError("Core Audio Tap sidecar のキャプチャ開始がタイムアウトしました")

    def _drain_sidecar_stderr(self) -> None:
        process = self._process
        stream = process.stderr if process is not None else None
        if stream is None:
            return
        try:
            for line in stream:
                text = line.rstrip()
                if not text:
                    continue
                self._stderr_lines.append(text)
                if len(self._stderr_lines) > 40:
                    self._stderr_lines = self._stderr_lines[-40:]
                if "ERROR" in text:
                    logger.warning("Core Audio Tap sidecar: %s", text)
                else:
                    logger.info("Core Audio Tap sidecar: %s", text)
        except Exception as e:
            logger.debug("Core Audio Tap stderr drain ended: %s", e)

    def _sidecar_error_tail(self) -> str:
        return "\n".join(self._stderr_lines[-8:]).strip() or "no stderr"

    def _tail_raw_audio(self) -> None:
        callback = self._external_callback
        if callback is None:
            return
        raw_path = self._raw_path
        offset = 0
        pending = b""
        frame_size = 4
        while True:
            try:
                size = raw_path.stat().st_size if raw_path.exists() else 0
                if size > offset:
                    with open(raw_path, "rb") as f:
                        f.seek(offset)
                        data = f.read(size - offset)
                    offset = size
                    if data:
                        payload = pending + data
                        aligned = (len(payload) // frame_size) * frame_size
                        if aligned > 0:
                            import numpy as np
                            samples = np.frombuffer(payload[:aligned], dtype="<f4").copy()
                            try:
                                callback(samples, self._sample_rate)
                            except Exception as e:
                                logger.error("Core Audio Tap PCM callback error: %s", e)
                        pending = payload[aligned:]
                if self._tail_stop.is_set():
                    latest_size = raw_path.stat().st_size if raw_path.exists() else offset
                    if latest_size <= offset:
                        break
                time.sleep(0.05)
            except Exception as e:
                if self._tail_stop.is_set():
                    break
                logger.warning("Core Audio Tap raw tail error: %s", e)
                time.sleep(0.1)

    def _stop_sidecar_process(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        finally:
            self._process = None

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._backend == "coreaudio_tap":
            self._stop_sidecar_process()
            self._tail_stop.set()
            if self._tail_thread:
                self._tail_thread.join(timeout=2)
                self._tail_thread = None

        # ffmpeg: raw float32 mono (config sample_rate) → WAV mono
        if self._raw_path and self._raw_path.exists():
            import subprocess
            try:
                result = subprocess.run([
                    _find_ffmpeg(), "-y",
                    "-f", "f32le", "-ar", str(self._sample_rate), "-ac", "1",
                    "-i", str(self._raw_path),
                    str(self._wav_path),
                ], capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    stderr = (result.stderr or "").strip()
                    raise RuntimeError(f"ffmpeg exited {result.returncode}: {stderr[-500:]}")
                if not (self._wav_path.exists() and self._wav_path.stat().st_size > 44):
                    raise RuntimeError("converted WAV was not created or is empty")
                self._raw_path.unlink(missing_ok=True)
                if self._meta_path:
                    self._meta_path.unlink(missing_ok=True)
                logger.info("System audio: raw → %s", self._wav_path.name)
            except Exception as e:
                self._error = f"System audio conversion failed: {e}"
                logger.error("System audio conversion failed: %s", e)

        self._process = None
        logger.info("System audio capture stopped")
