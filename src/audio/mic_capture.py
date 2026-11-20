"""Core Audio sidecar でマイク音声をキャプチャする。"""
from __future__ import annotations

from collections.abc import Callable
import json
import logging
import platform
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path

import numpy as np

from src.audio.system_capture import _find_audio_capture_sidecar

logger = logging.getLogger(__name__)

AudioCallback = Callable[..., None]
_RAW_FRAME_BYTES = 4


def is_available() -> bool:
    return platform.system() == "Darwin" and _find_audio_capture_sidecar() is not None


def list_microphones() -> dict:
    sidecar = _find_audio_capture_sidecar()
    if sidecar is None:
        raise RuntimeError("Core Audio mic sidecar が見つかりません")
    result = subprocess.run(
        [str(sidecar), "--list-input-devices", "--mode", "list"],
        capture_output=True, check=True, timeout=5,
    )
    return json.loads(result.stdout)


class _MicrophoneInput:
    """一つの入力プロセスと、その生音声ファイルの読み取り位置。"""

    def __init__(self, raw_path: Path, sample_rate: int) -> None:
        self.raw_path = raw_path
        self.meta_path = raw_path.with_suffix(".meta.json")
        self.sample_rate = sample_rate
        self.device_name: str | None = None
        self.device_uid: str | None = None
        self.offset = 0
        self.process: subprocess.Popen | None = None
        self._stderr_lines: list[str] = []

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, *, device_name: str | None = None, device_uid: str | None = None,
              wait_for_audio: bool = False) -> None:
        sidecar = _find_audio_capture_sidecar()
        if sidecar is None:
            raise RuntimeError("Core Audio mic sidecar が見つかりません")

        self.remove_files()
        command = [
            str(sidecar), str(self.raw_path), "--mode", "microphone",
            "--meta-path", str(self.meta_path), "--sample-rate", str(self.sample_rate),
        ]
        if device_uid:
            command += ["--mic-device-uid", device_uid]
        elif device_name:
            command += ["--mic-device-name", device_name]

        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        threading.Thread(
            target=self._read_stderr, args=(self.process.stderr,), daemon=True, name="coreaudio-mic-stderr",
        ).start()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not self.running:
                detail = "\n".join(self._stderr_lines[-8:]) or "no stderr"
                raise RuntimeError(f"マイクの起動に失敗しました: {detail}")
            if self.meta_path.exists() and (not wait_for_audio or self._size() >= _RAW_FRAME_BYTES):
                meta = json.loads(self.meta_path.read_text())
                if str(meta.get("format", "")).lower() != "f32le":
                    raise RuntimeError("マイクの音声形式が一致しません")
                self.sample_rate = max(8000, min(192000, int(float(meta.get("sample_rate") or self.sample_rate))))
                self.device_name = meta.get("device_name") or None
                self.device_uid = meta.get("device_uid") or None
                if device_uid is not None and self.device_uid != device_uid:
                    raise RuntimeError("選択したマイクに接続できませんでした")
                return
            time.sleep(0.05)
        raise RuntimeError("マイクから音声を受け取るまでに時間がかかりすぎました")

    def _read_stderr(self, stream) -> None:
        if stream is None:
            return
        for line in stream:
            message = line.rstrip()
            if message:
                self._stderr_lines.append(message)
                del self._stderr_lines[:-40]
                log = logger.warning if "ERROR" in message else logger.info
                log("Core Audio mic sidecar: %s", message)

    def _size(self) -> int:
        try:
            return self.raw_path.stat().st_size
        except FileNotFoundError:
            return 0

    def discard_buffered_audio(self) -> None:
        size = self._size()
        self.offset = size - size % _RAW_FRAME_BYTES

    def read_audio(self) -> np.ndarray | None:
        size = self._size() - self.offset
        count = size - size % _RAW_FRAME_BYTES
        if count <= 0:
            return None
        with self.raw_path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(count)
        length = len(data) - len(data) % _RAW_FRAME_BYTES
        if not length:
            return None
        self.offset += length
        return np.frombuffer(data[:length], dtype="<f4")

    def stop_process(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        self.process = None

    def remove_files(self) -> None:
        for path in (self.raw_path, self.meta_path):
            path.unlink(missing_ok=True)

    def close(self) -> None:
        self.stop_process()
        self.remove_files()


class CoreAudioMicCapture:
    def __init__(self) -> None:
        self._running = False
        self._input: _MicrophoneInput | None = None
        self._input_lock = threading.Lock()
        self._wav_path: Path | None = None
        self._wav_file: wave.Wave_write | None = None
        self._sample_rate = 16000
        self._error: str | None = None
        self._tail_thread: threading.Thread | None = None
        self._tail_stop = threading.Event()
        self._external_callback: AudioCallback | None = None
        self._level_callback: AudioCallback | None = None
        self._muted_getter: Callable[[], bool] | None = None

    @property
    def running(self) -> bool:
        source = self._input
        return bool(self._running and source and source.running)

    @property
    def device_info(self) -> tuple[str | None, str | None]:
        source = self._input
        return (source.device_name, source.device_uid) if source else (None, None)

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def backend(self) -> str:
        return "coreaudio_mic"

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

        source = _MicrophoneInput(output_path.with_suffix(".raw"), max(8000, min(48000, int(sample_rate or 16000))))
        try:
            source.start(device_name=device_name)
            wav_file = wave.open(str(output_path), "wb")
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(source.sample_rate)
        except Exception:
            source.close()
            raise

        self._input = source
        self._wav_path = output_path
        self._wav_file = wav_file
        self._sample_rate = source.sample_rate
        self._external_callback = external_callback
        self._level_callback = level_callback
        self._muted_getter = muted_getter
        self._error = None
        self._tail_stop.clear()
        self._running = True
        self._tail_thread = threading.Thread(
            target=self._tail_raw_audio, daemon=True, name="coreaudio-mic-raw-tail",
        )
        self._tail_thread.start()
        logger.info("Mic capture started (Core Audio sidecar %dHz, device=%s)", self._sample_rate, source.device_name)

    def switch_device(self, device_uid: str | None) -> None:
        previous = self._input
        if not self._running or previous is None or self._wav_path is None:
            raise RuntimeError("録音中ではありません")
        if device_uid is None:
            device_uid = list_microphones().get("default_uid")
            if device_uid is None:
                raise RuntimeError("システムの既定のマイクが見つかりません")
        if device_uid == previous.device_uid and previous.running:
            return

        path = self._wav_path.with_name(f"mic-{uuid.uuid4().hex}.raw")
        candidate = _MicrophoneInput(path, self._sample_rate)
        try:
            candidate.start(device_uid=device_uid, wait_for_audio=True)
            if candidate.sample_rate != self._sample_rate:
                raise RuntimeError("選択したマイクは現在の録音のサンプルレートに対応していません")
            with self._input_lock:
                if not candidate.running:
                    raise RuntimeError("選択したマイクから音声を受け取れませんでした")
                # 旧入力の末尾を録音へ書き、新入力の準備中に重複した音声を捨てる。
                samples = previous.read_audio()
                if samples is not None:
                    self._emit_samples(samples)
                candidate.discard_buffered_audio()
                self._input = candidate
        except Exception:
            candidate.close()
            raise

        try:
            previous.close()
        except Exception:
            logger.exception("Failed to close the previous microphone")
        logger.info("Microphone switched to %s", candidate.device_name)

    def _tail_raw_audio(self) -> None:
        while True:
            try:
                with self._input_lock:
                    samples = self._input.read_audio() if self._input else None
                    if samples is not None:
                        self._emit_samples(samples)
                    if self._tail_stop.is_set():
                        return
                time.sleep(0.03)
            except Exception as error:
                if self._tail_stop.is_set():
                    return
                logger.warning("Core Audio mic raw tail error: %s", error)
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

    def stop(self) -> None:
        source = self._input
        if source is None:
            return
        self._running = False
        try:
            source.stop_process()
        finally:
            with self._input_lock:
                self._tail_stop.set()
            if self._tail_thread is not None:
                self._tail_thread.join()
                self._tail_thread = None
            if self._wav_file is not None:
                self._wav_file.close()
                self._wav_file = None
            self._input = None
        source.remove_files()

        if not self._wav_path or not self._wav_path.exists() or self._wav_path.stat().st_size <= 44:
            self._error = "Mic capture produced no usable audio"
        logger.info("Mic capture stopped (Core Audio sidecar)")
