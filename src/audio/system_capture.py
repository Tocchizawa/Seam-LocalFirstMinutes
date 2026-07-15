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
AUDIO_PRESENT_RMS = 0.0001


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
        self._tail_stop: threading.Event | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._state_lock = threading.RLock()
        self._raw_format = "f32le"
        self._external_callback = None
        self._sidecar_path: Path | None = None
        self._read_offset = 0
        self._raw_bytes_seen = 0
        self._has_bytes = False
        self._has_nonzero_audio = False
        self._started_at = 0.0
        self._last_byte_at = 0.0
        self._last_nonzero_audio_at = 0.0
        self._last_nonzero_audio_offset = 0
        self._last_nonzero_audio_rms = 0.0
        self._max_rms = 0.0
        self._restart_count = 0
        self._restart_reasons: list[str] = []
        self._recovery_gap_sec = 0.0
        self._last_restart_at = 0.0
        self._segment_index = 0
        self._gap_index = 0
        self._segment_paths: list[Path] = []
        self._meta_paths: list[Path] = []

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def backend(self) -> str | None:
        return self._backend

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def recovery_reasons(self) -> list[str]:
        return list(self._restart_reasons)

    @property
    def recovery_gap_sec(self) -> float:
        return self._recovery_gap_sec

    def get_diagnostics(self) -> dict:
        now = time.monotonic()
        with self._state_lock:
            bytes_seen = self._current_raw_size()
            captured_duration = self._duration_for_bytes(bytes_seen)
            last_nonzero_sec = self._duration_for_bytes(self._last_nonzero_audio_offset)
            silent_tail_sec = None
            if self._has_nonzero_audio:
                silent_tail_sec = max(0.0, captured_duration - last_nonzero_sec)
            last_byte_age = (
                round(max(0.0, now - self._last_byte_at), 3)
                if self._last_byte_at else None
            )
            last_nonzero_age = (
                round(max(0.0, now - self._last_nonzero_audio_at), 3)
                if self._last_nonzero_audio_at else None
            )
            restart_reasons = list(self._restart_reasons)
        return {
            "backend": self._backend,
            "bytes": bytes_seen,
            "sample_rate": self._sample_rate,
            "raw_path": str(self._raw_path) if self._raw_path else None,
            "has_bytes": self._has_bytes,
            "has_audio": self._has_nonzero_audio,
            "captured_duration_sec": round(captured_duration, 3),
            "last_byte_age_sec": last_byte_age,
            "last_nonzero_audio_age_sec": last_nonzero_age,
            "last_nonzero_audio_sec": round(last_nonzero_sec, 3)
            if self._last_nonzero_audio_offset else None,
            "silent_tail_sec": round(silent_tail_sec, 3)
            if silent_tail_sec is not None else None,
            "last_nonzero_audio_rms": round(self._last_nonzero_audio_rms, 6)
            if self._last_nonzero_audio_rms else None,
            "max_rms": round(self._max_rms, 6) if self._max_rms else None,
            "restart_count": self._restart_count,
            "restart_reasons": restart_reasons,
            "gap_sec": round(self._recovery_gap_sec, 3),
            "segments": len(self._segment_paths),
        }

    def start(self, output_path: Path, sample_rate: int = 48000, external_callback=None) -> None:
        if self._running:
            raise RuntimeError("Already capturing")

        with self._state_lock:
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

        with self._state_lock:
            self._backend = "screencapturekit"
            self._sidecar_path = sidecar
            self._wav_path = output_path
            self._raw_path = None
            self._meta_path = None
            self._raw_format = "f32le"
            self._stderr_lines = []
            self._read_offset = 0
            self._raw_bytes_seen = 0
            self._has_bytes = False
            self._has_nonzero_audio = False
            self._started_at = time.monotonic()
            self._last_byte_at = 0.0
            self._last_nonzero_audio_at = 0.0
            self._last_nonzero_audio_offset = 0
            self._last_nonzero_audio_rms = 0.0
            self._max_rms = 0.0
            self._restart_count = 0
            self._restart_reasons = []
            self._recovery_gap_sec = 0.0
            self._last_restart_at = 0.0
            self._segment_index = 0
            self._gap_index = 0
            self._segment_paths = []
            self._meta_paths = []
            self._watchdog_stop.clear()
        self._cleanup_existing_outputs(output_path)
        self._start_segment(0)
        self._running = True
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="screencapturekit-audio-watchdog",
        )
        self._watchdog_thread.start()

        logger.info(
            "System audio capture started (ScreenCaptureKit %dHz -> %s)",
            self._sample_rate,
            output_path,
        )

    def _cleanup_existing_outputs(self, output_path: Path) -> None:
        paths = [
            output_path,
            output_path.with_suffix(".raw"),
            output_path.with_suffix(".meta.json"),
            output_path.with_suffix(".concat.raw"),
        ]
        paths.extend(output_path.parent.glob(f"{output_path.stem}.part*.raw"))
        paths.extend(output_path.parent.glob(f"{output_path.stem}.part*.meta.json"))
        paths.extend(output_path.parent.glob(f"{output_path.stem}.gap*.raw"))
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    def _segment_raw_path(self, index: int) -> Path:
        if self._wav_path is None:
            raise RuntimeError("system output path is not initialized")
        if index == 0:
            return self._wav_path.with_suffix(".raw")
        return self._wav_path.with_name(f"{self._wav_path.stem}.part{index}.raw")

    def _segment_meta_path(self, index: int) -> Path:
        if self._wav_path is None:
            raise RuntimeError("system output path is not initialized")
        if index == 0:
            return self._wav_path.with_suffix(".meta.json")
        return self._wav_path.with_name(f"{self._wav_path.stem}.part{index}.meta.json")

    def _gap_raw_path(self, index: int) -> Path:
        if self._wav_path is None:
            raise RuntimeError("system output path is not initialized")
        return self._wav_path.with_name(f"{self._wav_path.stem}.gap{index}.raw")

    def _start_segment(self, index: int, *, gap_started_at: float | None = None) -> None:
        sidecar = self._sidecar_path
        if sidecar is None:
            raise RuntimeError("audio-capture sidecar が見つかりません")

        raw_path = self._segment_raw_path(index)
        meta_path = self._segment_meta_path(index)
        for path in (raw_path, meta_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

        cmd = [
            str(sidecar),
            str(raw_path),
            "--mode",
            "screencapturekit",
            "--meta-path",
            str(meta_path),
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

        with self._state_lock:
            self._raw_path = raw_path
            self._meta_path = meta_path
            self._segment_index = index
            self._read_offset = 0
            self._tail_stop = threading.Event()

        self._stderr_thread = threading.Thread(
            target=self._drain_sidecar_stderr,
            daemon=True,
            name="screencapturekit-audio-stderr",
        )
        self._stderr_thread.start()

        self._wait_for_metadata()
        if gap_started_at is not None:
            self._append_recovery_gap(time.monotonic() - gap_started_at)
        with self._state_lock:
            self._segment_paths.append(raw_path)
            self._meta_paths.append(meta_path)
        self._tail_thread = threading.Thread(
            target=self._tail_raw_audio,
            args=(raw_path, self._tail_stop),
            daemon=True,
            name="screencapturekit-audio-raw-tail",
        )
        self._tail_thread.start()

    def _append_recovery_gap(self, duration_sec: float) -> None:
        duration_sec = max(0.0, float(duration_sec or 0.0))
        if duration_sec <= 0.02 or self._sample_rate <= 0:
            return
        frames = int(round(duration_sec * self._sample_rate))
        if frames <= 0:
            return
        gap_path = self._gap_raw_path(self._gap_index)
        self._gap_index += 1
        try:
            with open(gap_path, "wb") as f:
                remaining = frames * RAW_SAMPLE_WIDTH
                chunk = b"\x00" * min(remaining, self._sample_rate * RAW_SAMPLE_WIDTH)
                while remaining > 0:
                    n = min(remaining, len(chunk))
                    f.write(chunk[:n])
                    remaining -= n
            with self._state_lock:
                self._segment_paths.append(gap_path)
                self._raw_bytes_seen += frames * RAW_SAMPLE_WIDTH
                self._recovery_gap_sec += frames / self._sample_rate
        except Exception as e:
            logger.warning("Failed to preserve ScreenCaptureKit restart gap: %s", e)

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

    def _tail_raw_audio(self, raw_path: Path, stop_event: threading.Event | None) -> None:
        if raw_path is None:
            return

        pending = b""
        callback = self._external_callback
        read_offset = 0
        while True:
            try:
                size = raw_path.stat().st_size if raw_path.exists() else 0
                if size > read_offset:
                    with open(raw_path, "rb") as f:
                        f.seek(read_offset)
                        data = f.read(size - read_offset)
                    read_offset = size
                    with self._state_lock:
                        if raw_path == self._raw_path:
                            self._read_offset = read_offset
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

                if stop_event is not None and stop_event.is_set():
                    latest_size = raw_path.stat().st_size if raw_path.exists() else size
                    if latest_size <= read_offset:
                        break
                time.sleep(0.05)
            except Exception as e:
                if stop_event is not None and stop_event.is_set():
                    break
                logger.warning("ScreenCaptureKit raw tail error: %s", e)
                time.sleep(0.1)

    def _update_audio_stats(self, data: bytes) -> None:
        if not data:
            return
        now = time.monotonic()
        rms: float | None = None
        try:
            import numpy as np

            samples = np.frombuffer(data, dtype="<f4")
            if samples.size > 0:
                rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
        except Exception:
            pass
        with self._state_lock:
            self._has_bytes = True
            self._last_byte_at = now
            self._raw_bytes_seen += self._aligned_byte_count(len(data))
            if rms is not None:
                self._max_rms = max(self._max_rms, rms)
                if rms > AUDIO_PRESENT_RMS:
                    self._has_nonzero_audio = True
                    self._last_nonzero_audio_at = now
                    self._last_nonzero_audio_offset = self._raw_bytes_seen
                    self._last_nonzero_audio_rms = rms

    @staticmethod
    def _aligned_byte_count(value: int | None) -> int:
        if not value or value <= 0:
            return 0
        return int(value // RAW_SAMPLE_WIDTH * RAW_SAMPLE_WIDTH)

    def _current_raw_size(self) -> int:
        total = 0
        with self._state_lock:
            segment_paths = list(self._segment_paths)
            raw_path = self._raw_path
            raw_bytes_seen = self._raw_bytes_seen
        try:
            for path in segment_paths:
                if path.exists():
                    total += self._aligned_byte_count(path.stat().st_size)
            if not segment_paths and raw_path is not None and raw_path.exists():
                total += self._aligned_byte_count(raw_path.stat().st_size)
        except Exception:
            pass
        return max(self._aligned_byte_count(total), self._aligned_byte_count(raw_bytes_seen))

    def _duration_for_bytes(self, byte_count: int | None) -> float:
        if not byte_count or self._sample_rate <= 0:
            return 0.0
        return self._aligned_byte_count(byte_count) / (self._sample_rate * RAW_SAMPLE_WIDTH)

    def _watchdog_settings(self) -> tuple[bool, float, float, float, int]:
        cfg = config.get("recording", "system_capture_watchdog", default={}) or {}
        enabled = bool(cfg.get("enabled", True))
        try:
            min_active_sec = float(cfg.get("min_active_sec_before_restart", 30))
        except Exception:
            min_active_sec = 30.0
        try:
            silent_restart_sec = float(cfg.get("silent_restart_sec", cfg.get("max_missing_sec", 20)))
        except Exception:
            silent_restart_sec = 20.0
        try:
            restart_cooldown_sec = float(cfg.get("restart_cooldown_sec", 10))
        except Exception:
            restart_cooldown_sec = 10.0
        try:
            max_restarts = int(cfg.get("max_restarts", 5))
        except Exception:
            max_restarts = 5
        return (
            enabled,
            max(5.0, min(600.0, min_active_sec)),
            max(5.0, min(600.0, silent_restart_sec)),
            max(1.0, min(600.0, restart_cooldown_sec)),
            max(0, min(20, max_restarts)),
        )

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(1.0):
            enabled, min_active_sec, silent_restart_sec, restart_cooldown_sec, max_restarts = (
                self._watchdog_settings()
            )
            if not enabled or max_restarts <= 0:
                continue
            with self._state_lock:
                if not self._running:
                    continue
                if not self._has_nonzero_audio:
                    continue
                captured_duration = self._duration_for_bytes(self._raw_bytes_seen)
                last_nonzero_sec = self._duration_for_bytes(self._last_nonzero_audio_offset)
                silent_tail_sec = max(0.0, captured_duration - last_nonzero_sec)
                cooldown = (
                    self._last_restart_at <= 0
                    or (time.monotonic() - self._last_restart_at) >= restart_cooldown_sec
                )
                can_restart = self._restart_count < max_restarts
            if captured_duration < min_active_sec:
                continue
            if silent_tail_sec < silent_restart_sec or not cooldown or not can_restart:
                continue
            self._restart_for_silence(silent_tail_sec, max_restarts)

    def _restart_for_silence(self, silent_tail_sec: float, max_restarts: int) -> bool:
        with self._state_lock:
            if not self._running or self._restart_count >= max_restarts:
                return False
            next_index = self._segment_index + 1
            self._restart_count += 1
            self._last_restart_at = time.monotonic()
            reason = f"silent_tail_sec={silent_tail_sec:.1f}"
            self._restart_reasons.append(reason)
            if len(self._restart_reasons) > 20:
                self._restart_reasons = self._restart_reasons[-20:]
            attempt = self._restart_count
        logger.warning(
            "System audio silent for %.1fs; restarting ScreenCaptureKit (%d/%d)",
            silent_tail_sec,
            attempt,
            max_restarts,
        )
        gap_started_at = time.monotonic()
        try:
            self._stop_active_segment()
            if self._watchdog_stop.is_set():
                return False
            self._start_segment(next_index, gap_started_at=gap_started_at)
            logger.warning(
                "System audio ScreenCaptureKit restarted (%d/%d)",
                attempt,
                max_restarts,
            )
            return True
        except Exception as e:
            self._error = f"ScreenCaptureKit restart failed: {e}"
            logger.error("System audio ScreenCaptureKit restart failed: %s", e)
            return False

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

    def _stop_active_segment(self) -> None:
        tail_stop = self._tail_stop
        tail_thread = self._tail_thread
        self._stop_sidecar_process()
        if tail_stop is not None:
            tail_stop.set()
        if tail_thread is not None:
            tail_thread.join(timeout=2)
        with self._state_lock:
            if self._tail_thread is tail_thread:
                self._tail_thread = None
            if self._tail_stop is tail_stop:
                self._tail_stop = None

    def _raw_input_for_conversion(self) -> Path | None:
        if self._wav_path is None:
            return None
        with self._state_lock:
            candidates = list(self._segment_paths)
            raw_path = self._raw_path
        if not candidates and raw_path is not None:
            candidates = [raw_path]
        segments = [
            path for path in candidates
            if path.exists() and self._aligned_byte_count(path.stat().st_size) > 0
        ]
        if not segments:
            return None
        if len(segments) == 1:
            return segments[0]

        concat_path = self._wav_path.with_suffix(".concat.raw")
        try:
            concat_path.unlink(missing_ok=True)
        except Exception:
            pass
        with open(concat_path, "wb") as out:
            for path in segments:
                aligned = self._aligned_byte_count(path.stat().st_size)
                if aligned <= 0:
                    continue
                with open(path, "rb") as src:
                    remaining = aligned
                    while remaining > 0:
                        chunk = src.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                        out.write(chunk)
                        remaining -= len(chunk)
        return concat_path

    def _cleanup_converted_intermediates(self, input_path: Path) -> None:
        paths: set[Path] = set()
        with self._state_lock:
            paths.update(self._segment_paths)
            paths.update(self._meta_paths)
            if self._raw_path is not None:
                paths.add(self._raw_path)
            if self._meta_path is not None:
                paths.add(self._meta_path)
        paths.add(input_path)
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("Failed to remove ScreenCaptureKit intermediate %s: %s", path, e)

    def _convert_raw_to_wav(self) -> None:
        if self._wav_path is None:
            return
        input_path = self._raw_input_for_conversion()
        if input_path is None or self._current_raw_size() <= 0:
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
                str(input_path),
                "-c:a",
                "pcm_s16le",
                str(self._wav_path),
            ], capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                raise RuntimeError(f"ffmpeg exited {result.returncode}: {stderr[-500:]}")
            if not (self._wav_path.exists() and self._wav_path.stat().st_size > 44):
                raise RuntimeError("converted WAV was not created or is empty")
            self._cleanup_converted_intermediates(input_path)
            logger.info("System audio: ScreenCaptureKit raw -> %s", self._wav_path.name)
        except Exception as e:
            self._error = f"System audio conversion failed: {e}"
            logger.error("System audio conversion failed: %s", e)

    def stop(self) -> None:
        if not self._running:
            return
        with self._state_lock:
            self._running = False
        self._watchdog_stop.set()
        watchdog_thread = self._watchdog_thread
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=2)
            self._watchdog_thread = None

        self._stop_active_segment()
        self._convert_raw_to_wav()
        logger.info("System audio capture stopped")
