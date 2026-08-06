from __future__ import annotations

import math
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from src.audio.leveling import AdaptiveSpeechGain
from src.audio.mixed_writer import RealtimeMixedAudioWriter
from src.audio.recorder import FFMPEG


def _tone(rms: float, seconds: float, freq: float = 440.0, sample_rate: int = 16000) -> np.ndarray:
    t = np.arange(int(sample_rate * seconds), dtype=np.float32) / sample_rate
    return (math.sqrt(2.0) * rms * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


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

    def test_realtime_mixed_writer_finalizes_dispatched_audio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            writer = RealtimeMixedAudioWriter(Path(td))
            samples = np.concatenate([
                _tone(0.04, 1.0, freq=440),
                _tone(0.008, 1.0, freq=660),
            ])

            writer.start()
            writer.feed(samples[:16000])
            writer.feed(samples[16000:])
            combined = writer.finalize()

            self.assertIsNotNone(combined)
            self.assertEqual(Path(combined).name, "combined.flac")
            self.assertFalse((Path(td) / "combined.wav").exists())
            self.assertFalse((Path(td) / "combined.play.wav").exists())
            audio = _read_audio(Path(combined))
            self.assertAlmostEqual(len(audio) / 16000, 2.0, delta=0.05)
            self.assertGreater(_rms(audio[:16000]), _rms(audio[16000:]) * 3.0)


if __name__ == "__main__":
    unittest.main()
