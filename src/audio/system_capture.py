"""ScreenCaptureKit sidecar でシステム内部音声をキャプチャする。"""
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

SCREEN_CAPTURE_KIT_MIN_VERSION = (13, 0)
RAW_SAMPLE_WIDTH = 4


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
        raw = config.get("recording", "system_capture", default="screencapturekit")
    method = str(raw or "screencapturekit").strip().lower().replace("-", "_")
    aliases = {
        "sck": "screencapturekit",
        "screen_capture_kit": "screencapturekit",
        "screen_capturekit": "screencapturekit",
    }
    return aliases.get(method, method if method else "screencapturekit")


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


def _screen_capture_kit_os_supported() -> bool:
    if platform.system() != "Darwin":
        return False
    return _macos_version_tuple() >= SCREEN_CAPTURE_KIT_MIN_VERSION


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


def _screen_capture_kit_available() -> bool:
    return _screen_capture_kit_os_supported() and _find_audio_capture_sidecar() is not None


def is_available() -> bool:
    return _normalize_capture_method() == "screencapturekit" and _screen_capture_kit_available()


class SystemAudioCapture:
    def __init__(self) -> None:
        self._running = False
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
        self._sidecar_path: Path | None = None
        self._read_offset = 0
        self._raw_bytes_seen = 0
        self._has_bytes = False
        self._has_nonzero_audio = False
        self._started_at = 0.0
        self._last_byte_at = 0.0

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def backend(self) -> str | None:
        return self._backend

    @property
    def restart_count(self) -> int:
        return 0

    @property
    def recovery_reasons(self) -> list[str]:
        return []

    @property
    def recovery_gap_sec(self) -> float:
        return 0.0

    def get_diagnostics(self) -> dict:
        return {
            "backend": self._backend,
            "bytes": self._current_raw_size(),
            "sample_rate": self._sample_rate,
            "raw_path": str(self._raw_path) if self._raw_path else None,
            "has_bytes": self._has_bytes,
            "has_audio": self._has_nonzero_audio,
            "last_byte_age_sec": round(max(0.0, time.monotonic() - self._last_byte_at), 3)
            if self._last_byte_at else None,
            "restart_count": 0,
            "restart_reasons": [],
            "gap_sec": 0.0,
        }

    def start(self, output_path: Path, sample_rate: int = 48000, external_callback=None) -> None:
        if self._running:
            raise RuntimeError("Already capturing")

        self._error = None
        self._backend = None
        self._external_callback = external_callback
        self._sample_rate = max(8000, min(192000, int(sample_rate or 48000)))

        method = _normalize_capture_method()
        if method != "screencapturekit":
            self._error = f"未対応の内部音声キャプチャ方式です: {method}"
            raise RuntimeError(self._error)

        try:
            self._start_screen_capture_kit(output_path)
        except Exception as e:
            self._error = f"ScreenCaptureKit のキャプチャ開始に失敗: {e}"
            self._running = False
            self._stop_sidecar_process()
            raise RuntimeError(self._error) from e

    def _start_screen_capture_kit(self, output_path: Path) -> None:
        if not _screen_capture_kit_os_supported():
            raise RuntimeError("ScreenCaptureKit 内部音声キャプチャは macOS 13.0+ でのみ利用できます")

        sidecar = _find_audio_capture_sidecar()
        if sidecar is None:
            raise RuntimeError("audio-capture sidecar が見つかりません")

        self._backend = "screencapturekit"
        self._sidecar_path = sidecar
        self._wav_path = output_path
        self._raw_path = output_path.with_suffix(".raw")
        self._meta_path = output_path.with_suffix(".meta.json")
        self._raw_format = "f32le"
        self._stderr_lines = []
        self._tail_stop.clear()
        self._read_offset = 0
        self._raw_bytes_seen = 0
        self._has_bytes = False
        self._has_nonzero_audio = False
        self._started_at = time.monotonic()
        self._last_byte_at = 0.0

        for path in (self._raw_path, self._meta_path, self._wav_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

        cmd = [
            str(sidecar),
            str(self._raw_path),
            "--mode",
            "screencapturekit",
            "--meta-path",
            str(self._meta_path),
            "--sample-rate",
            str(self._sample_rate),
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
            raise RuntimeError(f"audio-capture sidecar の起動に失敗: {e}") from e

        self._stderr_thread = threading.Thread(
            target=self._drain_sidecar_stderr,
            daemon=True,
            name="screencapturekit-audio-stderr",
        )
        self._stderr_thread.start()

        self._wait_for_metadata()
        self._running = True
        self._tail_thread = threading.Thread(
            target=self._tail_raw_audio,
            daemon=True,
            name="screencapturekit-audio-raw-tail",
        )
        self._tail_thread.start()

        logger.info(
            "System audio capture started (ScreenCaptureKit %dHz -> %s)",
            self._sample_rate,
            output_path,
        )

    def _wait_for_metadata(self) -> None:
        if self._meta_path is None:
            raise RuntimeError("metadata path is not initialized")

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._meta_path.exists():
                try:
                    meta = json.loads(self._meta_path.read_text())
                    raw_format = str(meta.get("format") or "f32le").lower()
                    if raw_format != "f32le":
                        raise RuntimeError(f"unsupported sidecar format: {raw_format}")
                    channels = int(meta.get("channels") or 1)
                    if channels != 1:
                        raise RuntimeError(f"unsupported sidecar channel count: {channels}")
                    self._raw_format = raw_format
                    self._sample_rate = max(8000, min(192000, int(float(meta.get("sample_rate") or 48000))))
                    return
                except Exception as e:
                    raise RuntimeError(f"ScreenCaptureKit metadata の読み込みに失敗: {e}") from e

            process = self._process
            code = process.poll() if process is not None else None
            if code is not None:
                raise RuntimeError(f"ScreenCaptureKit sidecar exited early ({code}): {self._sidecar_error_tail()}")
            time.sleep(0.05)

        raise RuntimeError("ScreenCaptureKit sidecar のキャプチャ開始がタイムアウトしました")

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
                    logger.warning("ScreenCaptureKit sidecar: %s", text)
                else:
                    logger.info("ScreenCaptureKit sidecar: %s", text)
        except Exception as e:
            logger.debug("ScreenCaptureKit stderr drain ended: %s", e)

    def _sidecar_error_tail(self) -> str:
        return "\n".join(self._stderr_lines[-8:]).strip() or "no stderr"

    def _tail_raw_audio(self) -> None:
        raw_path = self._raw_path
        if raw_path is None:
            return

        pending = b""
        callback = self._external_callback
        while True:
            try:
                size = raw_path.stat().st_size if raw_path.exists() else 0
                if size > self._read_offset:
                    with open(raw_path, "rb") as f:
                        f.seek(self._read_offset)
                        data = f.read(size - self._read_offset)
                    self._read_offset = size
                    payload = pending + data
                    aligned = (len(payload) // RAW_SAMPLE_WIDTH) * RAW_SAMPLE_WIDTH
                    if aligned > 0:
                        chunk = payload[:aligned]
                        self._update_audio_stats(chunk)
                        if callback is not None:
                            import numpy as np

                            samples = np.frombuffer(chunk, dtype="<f4").copy()
                            try:
                                callback(samples, self._sample_rate)
                            except Exception as e:
                                logger.error("ScreenCaptureKit PCM callback error: %s", e)
                    pending = payload[aligned:]

                if self._tail_stop.is_set():
                    latest_size = raw_path.stat().st_size if raw_path.exists() else size
                    if latest_size <= self._read_offset:
                        break
                time.sleep(0.05)
            except Exception as e:
                if self._tail_stop.is_set():
                    break
                logger.warning("ScreenCaptureKit raw tail error: %s", e)
                time.sleep(0.1)

    def _update_audio_stats(self, data: bytes) -> None:
        if not data:
            return
        now = time.monotonic()
        self._has_bytes = True
        self._last_byte_at = now
        self._raw_bytes_seen = max(self._raw_bytes_seen, self._read_offset)
        try:
            import numpy as np

            samples = np.frombuffer(data, dtype="<f4")
            if samples.size == 0:
                return
            rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
            if rms > 0.0001:
                self._has_nonzero_audio = True
        except Exception:
            pass

    @staticmethod
    def _aligned_byte_count(value: int | None) -> int:
        if not value or value <= 0:
            return 0
        return int(value // RAW_SAMPLE_WIDTH * RAW_SAMPLE_WIDTH)

    def _current_raw_size(self) -> int:
        try:
            if self._raw_path is not None and self._raw_path.exists():
                return self._aligned_byte_count(self._raw_path.stat().st_size)
        except Exception:
            pass
        return self._aligned_byte_count(self._raw_bytes_seen)

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

    def _convert_raw_to_wav(self) -> None:
        if self._raw_path is None or self._wav_path is None:
            return
        if not self._raw_path.exists() or self._current_raw_size() <= 0:
            self._error = "System audio conversion failed: no captured ScreenCaptureKit audio data"
            logger.error(self._error)
            return

        try:
            result = subprocess.run([
                _find_ffmpeg(),
                "-y",
                "-f",
                "f32le",
                "-ar",
                str(self._sample_rate),
                "-ac",
                "1",
                "-i",
                str(self._raw_path),
                "-c:a",
                "pcm_s16le",
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
            logger.info("System audio: ScreenCaptureKit raw -> %s", self._wav_path.name)
        except Exception as e:
            self._error = f"System audio conversion failed: {e}"
            logger.error("System audio conversion failed: %s", e)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        self._stop_sidecar_process()
        self._tail_stop.set()
        if self._tail_thread:
            self._tail_thread.join(timeout=2)
            self._tail_thread = None
        self._convert_raw_to_wav()
        logger.info("System audio capture stopped")
