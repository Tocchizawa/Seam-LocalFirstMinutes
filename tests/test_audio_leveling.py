from __future__ import annotations

import math
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from src.audio.leveling import AdaptiveSpeechGain, build_ffmpeg_loudness_filter
from src.audio.recorder import FFMPEG, Recorder
from src.config import config


def _tone(rms: float, seconds: float, freq: float = 440.0, sample_rate: int = 16000) -> np.ndarray:
    t = np.arange(int(sample_rate * seconds), dtype=np.float32) / sample_rate
    return (math.sqrt(2.0) * rms * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = 16000) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def _read_audio(path: Path, sample_rate: int = 16000) -> np.ndarray:
    proc = subprocess.run(
        [
            FFMPEG, "-loglevel", "error", "-i", str(path),
            "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


class AudioLevelingTest(unittest.TestCase):
    def test_adaptive_gain_boosts_speech_after_level_drop(self) -> None:
        gain = AdaptiveSpeechGain(
            sample_rate=16000,
            target_rms=0.08,
            noise_floor=0.003,
            max_gain=8.0,
            attack=0.25,
            release=0.55,
            peak_limit=0.95,
        )
        gain.process(_tone(0.06, 1.0))
        quiet = _tone(0.006, 2.0)

        boosted = gain.process(quiet)

        self.assertGreater(_rms(boosted), _rms(quiet) * 2.5)
        self.assertGreater(_rms(boosted), 0.02)
        self.assertLessEqual(float(np.max(np.abs(boosted))), 0.95)

    def test_adaptive_gain_does_not_raise_near_silence(self) -> None:
        gain = AdaptiveSpeechGain(
            sample_rate=16000,
            target_rms=0.08,
            noise_floor=0.003,
            max_gain=8.0,
            attack=0.25,
            release=0.55,
            peak_limit=0.95,
        )
        quiet_noise = _tone(0.001, 1.0)

        out = gain.process(quiet_noise)

        self.assertLess(_rms(out), 0.002)
        self.assertLess(gain.last_gain, 1.1)

    def test_final_mix_command_uses_dynamic_normalization_and_unscaled_amix(self) -> None:
        loudness = build_ffmpeg_loudness_filter({
            "enabled": True,
            "final_normalize": True,
            "target_rms": 0.08,
            "noise_floor": 0.003,
            "max_gain": 12.0,
        })
        self.assertIsNotNone(loudness)
        with tempfile.TemporaryDirectory() as td:
            r = Recorder()
            r._session_dir = Path(td)
            cmd = r._build_finalize_cmd(
                Path(td) / "mic.wav",
                Path(td) / "system.wav",
                delay_ms=250,
                loudness_filter=loudness,
            )

        joined = " ".join(cmd)
        self.assertIn("adelay=250", joined)
        self.assertIn("amix=inputs=2:duration=longest:normalize=0", joined)
        self.assertIn("dynaudnorm=", joined)
        self.assertIn("alimiter=", joined)

    def test_finalize_recovers_late_recording_level_drop(self) -> None:
        recording_cfg = config._data.setdefault("recording", {})
        old_leveling = dict(recording_cfg.get("audio_leveling") or {})
        old_timeout = recording_cfg.get("finalize_timeout_sec")
        recording_cfg["audio_leveling"] = {
            "enabled": True,
            "realtime_enabled": True,
            "final_normalize": True,
            "target_rms": 0.08,
            "noise_floor": 0.003,
            "max_gain": 12.0,
            "peak_limit": 0.95,
            "frame_ms": 100,
            "gauss_size": 3,
        }
        recording_cfg["finalize_timeout_sec"] = 300
        try:
            with tempfile.TemporaryDirectory() as td:
                session_dir = Path(td)
                mic = np.concatenate([
                    _tone(0.04, 3.0, freq=440),
                    _tone(0.004, 3.0, freq=440),
                ])
                system = np.concatenate([
                    _tone(0.04, 3.0, freq=660),
                    _tone(0.004, 3.0, freq=660),
                ])
                _write_wav(session_dir / "mic.wav", mic)
                _write_wav(session_dir / "system.wav", system)

                recorder = Recorder()
                recorder._session_dir = session_dir
                combined = recorder._finalize_audio(
                    session_dir / "mic.wav",
                    session_dir / "system.wav",
                )

                self.assertIsNotNone(combined)
                audio = _read_audio(combined)
                first = _rms(audio[:3 * 16000])
                second = _rms(audio[3 * 16000:6 * 16000])
                self.assertGreater(second / first, 0.5)
        finally:
            recording_cfg["audio_leveling"] = old_leveling
            if old_timeout is None:
                recording_cfg.pop("finalize_timeout_sec", None)
            else:
                recording_cfg["finalize_timeout_sec"] = old_timeout


if __name__ == "__main__":
    unittest.main()
