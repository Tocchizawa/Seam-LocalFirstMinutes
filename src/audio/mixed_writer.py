from __future__ import annotations

import logging
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np

from src.audio.recorder import FFMPEG

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class RealtimeMixedAudioWriter:
    """Persist the exact mixed stream sent to Whisper."""

    def __init__(self, session_dir: Path, *, sample_rate: int = SAMPLE_RATE) -> None:
        self.session_dir = session_dir
        self.sample_rate = int(sample_rate)
        self.wav_path = session_dir / "combined.wav"
        self.flac_path = session_dir / "combined.flac"
        self._lock = threading.Lock()
        self._wav: wave.Wave_write | None = None
        self._samples_written = 0
        self._closed = False

    @property
    def duration_sec(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self._samples_written / self.sample_rate

    def start(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.wav_path, self.flac_path, self.session_dir / "combined.play.wav"):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        wav = wave.open(str(self.wav_path), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(self.sample_rate)
        with self._lock:
            self._wav = wav
            self._samples_written = 0
            self._closed = False

    def feed(self, samples_f32: np.ndarray) -> None:
        if samples_f32 is None or len(samples_f32) == 0:
            return
        with self._lock:
            if self._closed or self._wav is None:
                return
            arr = np.asarray(samples_f32, dtype=np.float32)
            arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
            pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")
            self._wav.writeframes(pcm.tobytes())
            self._samples_written += int(pcm.size)

    def close(self) -> None:
        with self._lock:
            wav = self._wav
            self._wav = None
            self._closed = True
        if wav is not None:
            try:
                wav.close()
            except Exception as e:
                logger.warning("Failed to close realtime mixed WAV: %s", e)

    def finalize(self, *, timeout_sec: int = 300) -> Path | None:
        self.close()
        if self._samples_written <= 0:
            logger.warning("Realtime mixed audio is empty")
            return None
        if not (self.wav_path.exists() and self.wav_path.stat().st_size > 44):
            logger.warning("Realtime mixed WAV missing or empty: %s", self.wav_path)
            return None

        cmd = [
            FFMPEG, "-y",
            "-i", str(self.wav_path),
            "-ar", "24000", "-ac", "1",
            "-c:a", "flac", "-compression_level", "8",
            str(self.flac_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(60, int(timeout_sec)),
            )
            if result.returncode != 0:
                logger.error(
                    "realtime mixed FLAC finalize failed (code %d): %s",
                    result.returncode,
                    (result.stderr or "")[-500:],
                )
                return self.wav_path
            if not (self.flac_path.exists() and self.flac_path.stat().st_size > 44):
                logger.error("Realtime mixed FLAC not created or empty")
                return self.wav_path
            try:
                self.wav_path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("Failed to remove realtime mixed WAV: %s", e)
            logger.info(
                "Realtime mixed audio finalized -> %s (%.1f MB, %.1fs)",
                self.flac_path.name,
                self.flac_path.stat().st_size / (1024 * 1024),
                self.duration_sec,
            )
            return self.flac_path
        except Exception as e:
            logger.error("Realtime mixed audio finalize failed: %s", e)
            return self.wav_path
