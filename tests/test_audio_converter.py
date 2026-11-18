from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
import subprocess

from src.api import recording
from src.audio.converter import convert_audio_to_mp3
from src.audio.recorder import FFMPEG, _audio_duration_sec


class AudioConverterTest(unittest.TestCase):
    def test_convert_audio_to_mp3_keeps_source_and_duration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.wav"
            destination = root / "combined.mp3"
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x00\x00" * 16000)

            result = convert_audio_to_mp3(
                source,
                destination,
                ffmpeg_bin=FFMPEG,
            )

            self.assertEqual(result, destination)
            self.assertTrue(source.exists())
            self.assertGreater(destination.stat().st_size, 44)
            self.assertAlmostEqual(_audio_duration_sec(destination), 1.0, delta=0.1)

    def test_legacy_flac_gets_seekable_playback_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_wav = root / "source.wav"
            source_flac = root / "combined.flac"
            with wave.open(str(source_wav), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b"\x00\x00" * 24000)
            subprocess.run(
                [FFMPEG, "-loglevel", "error", "-y", "-i", str(source_wav), str(source_flac)],
                check=True,
            )

            selected = recording._pick_playback_audio(root)

            self.assertEqual(selected, root / "combined.play.mp3")
            self.assertTrue(source_flac.exists())
            self.assertAlmostEqual(_audio_duration_sec(selected), 1.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
