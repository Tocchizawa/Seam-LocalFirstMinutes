"""Core Audio sidecar でマイク音声をキャプチャする。"""
from __future__ import annotations

from collections.abc import Callable
import json
import logging
import platform
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np

from src.audio.system_capture import _find_audio_capture_sidecar

logger = logging.getLogger(__name__)

AudioCallback = Callable[..., None]


def is_available() -> bool:
    return platform.system() == "Darwin" and _find_audio_capture_sidecar() is not None


class CoreAudioMicCapture:
    def __init__(self) -> None:
        self._running = False
        self._raw_path: Path | None = None
        self._wav_path: Path | None = None
        self._meta_path: Path | None = None
        self._wav_file: wave.Wave_write | None = None
        self._sample_rate = 16000
        self._backend: str | None = None
        self._error: str | None = None
        self._process: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: list[str] = []
        self._tail_thread: threading.Thread | None = None
        self._tail_stop = threading.Event()
        self._external_callback: AudioCallback | None = None
        self._level_callback: AudioCallback | None = None
        self._muted_getter: Callable[[], bool] | None = None

    @property
    def running(self) -> bool:
        if self._process is not None and self._process.poll() is not None:
            return False
        return self._running

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def backend(self) -> str | None:
        return self._backend

    def start(
        self,
        output_path: Path,
        *,
        sample_rate: int = 16000,
        device_name: str | None = None,
        external_callback: AudioCallback | None = None,
        level_callback: AudioCallback | None = None,
        muted_getter: Callable[[], bool] | None = None,
    ) -> None:
        if self._running:
            raise RuntimeError("Already capturing")
        if platform.system() != "Darwin":
            raise RuntimeError("Core Audio mic sidecar は macOS でのみ利用できます")

        sidecar = _find_audio_capture_sidecar()
        if sidecar is None:
            raise RuntimeError("Core Audio mic sidecar が見つかりません")

        self._error = None
        self._backend = "coreaudio_mic"
        self._external_callback = external_callback
        self._level_callback = level_callback
        self._muted_getter = muted_getter
        self._raw_path = output_path.with_suffix(".raw")
        self._wav_path = output_path
        self._meta_path = output_path.with_suffix(".meta.json")
        self._stderr_lines = []
        self._tail_stop.clear()
        self._sample_rate = max(8000, min(48000, int(sample_rate or 16000)))

        for path in (self._raw_path, self._meta_path, self._wav_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

        cmd = [
            str(sidecar),
            str(self._raw_path),
            "--mode",
            "microphone",
            "--meta-path",
            str(self._meta_path),
            "--sample-rate",
            str(self._sample_rate),
        ]
        if device_name:
            cmd += ["--mic-device-name", device_name]

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
            raise RuntimeError(f"Core Audio mic sidecar の起動に失敗: {e}") from e

        self._stderr_thread = threading.Thread(
            target=self._drain_sidecar_stderr,
            daemon=True,
            name="coreaudio-mic-stderr",
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
                    self._sample_rate = max(8000, min(192000, int(float(meta.get("sample_rate") or self._sample_rate))))
                except Exception as e:
                    self._stop_sidecar_process()
                    raise RuntimeError(f"Core Audio mic metadata の読み込みに失敗: {e}") from e

                self._wav_file = wave.open(str(self._wav_path), "wb")
                self._wav_file.setnchannels(1)
                self._wav_file.setsampwidth(2)
                self._wav_file.setframerate(self._sample_rate)

                self._tail_thread = threading.Thread(
                    target=self._tail_raw_audio,
                    daemon=True,
                    name="coreaudio-mic-raw-tail",
                )
                self._tail_thread.start()
                self._running = True
                logger.info(
                    "Mic capture started (Core Audio sidecar %dHz, device=%s → %s)",
                    self._sample_rate,
                    device_name or "default",
                    output_path,
                )
                return

            code = self._process.poll() if self._process is not None else None
            if code is not None:
                err = self._sidecar_error_tail()
                self._stop_sidecar_process()
                raise RuntimeError(f"Core Audio mic sidecar exited early ({code}): {err}")
            time.sleep(0.05)

        self._stop_sidecar_process()
        raise RuntimeError("Core Audio mic sidecar のキャプチャ開始がタイムアウトしました")

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
                    logger.warning("Core Audio mic sidecar: %s", text)
                else:
                    logger.info("Core Audio mic sidecar: %s", text)
        except Exception as e:
            logger.debug("Core Audio mic stderr drain ended: %s", e)

    def _sidecar_error_tail(self) -> str:
        return "\n".join(self._stderr_lines[-8:]).strip() or "no stderr"

    def _tail_raw_audio(self) -> None:
        raw_path = self._raw_path
        if raw_path is None:
            return
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
                            samples = np.frombuffer(payload[:aligned], dtype="<f4").copy()
                            self._emit_samples(samples)
                        pending = payload[aligned:]
                if self._tail_stop.is_set():
                    latest_size = raw_path.stat().st_size if raw_path.exists() else offset
                    if latest_size <= offset:
                        break
                time.sleep(0.03)
            except Exception as e:
                if self._tail_stop.is_set():
                    break
                logger.warning("Core Audio mic raw tail error: %s", e)
                time.sleep(0.1)

    def _emit_samples(self, samples_f32: np.ndarray) -> None:
        if samples_f32.size == 0:
            return
        try:
            muted = bool(self._muted_getter()) if self._muted_getter is not None else False
        except Exception:
            muted = False
        if muted:
            samples_f32 = np.zeros_like(samples_f32)

        clipped = np.clip(samples_f32.astype(np.float32, copy=False), -1.0, 1.0)
        samples_i16 = (clipped * 32767.0).astype("<i2")

        if self._wav_file is not None:
            try:
                self._wav_file.writeframes(samples_i16.tobytes())
            except Exception as e:
                logger.error("Mic WAV write error: %s", e)

        if self._external_callback is not None:
            try:
                self._external_callback(samples_i16, self._sample_rate)
            except Exception as e:
                logger.error("Mic PCM callback error: %s", e)

        if self._level_callback is not None:
            try:
                if muted:
                    level = 0.0
                else:
                    rms = float(np.sqrt(np.mean(clipped ** 2)))
                    level = min(1.0, rms * 5.0)
                self._level_callback(level)
            except Exception:
                pass

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
        if not self._running and self._process is None:
            return
        self._running = False

        self._stop_sidecar_process()
        self._tail_stop.set()
        if self._tail_thread:
            self._tail_thread.join(timeout=3)
            self._tail_thread = None

        if self._wav_file is not None:
            try:
                self._wav_file.close()
            except Exception:
                pass
            self._wav_file = None

        if self._raw_path and self._raw_path.exists():
            try:
                self._raw_path.unlink(missing_ok=True)
            except Exception:
                pass
        if self._meta_path and self._meta_path.exists():
            try:
                self._meta_path.unlink(missing_ok=True)
            except Exception:
                pass

        wav_ok = self._wav_path and self._wav_path.exists() and self._wav_path.stat().st_size > 44
        if not wav_ok:
            self._error = "Mic capture produced no usable audio"
        logger.info("Mic capture stopped (Core Audio sidecar)")
