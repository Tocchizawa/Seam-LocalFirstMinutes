"""Small audio conversion helpers shared by recording and migration paths."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


DEFAULT_MP3_BITRATE = "64k"


def convert_audio_to_mp3(
    source: Path,
    destination: Path,
    *,
    ffmpeg_bin: str | None = None,
    sample_rate: int = 16000,
    channels: int = 1,
    bitrate: str = DEFAULT_MP3_BITRATE,
    timeout_sec: int = 900,
) -> Path:
    """Convert one audio file to a seekable MP3 and return its path.

    The destination is removed when FFmpeg fails or produces an empty file.
    The source is never modified or removed.
    """
    source = Path(source)
    destination = Path(destination)
    if not source.is_file() or source.stat().st_size <= 0:
        raise FileNotFoundError(f"Audio source is missing or empty: {source}")
    if source.resolve() == destination.resolve():
        raise ValueError("Audio source and destination must be different")
    if sample_rate <= 0 or channels <= 0:
        raise ValueError("sample_rate and channels must be positive")

    destination.parent.mkdir(parents=True, exist_ok=True)
    binary = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        binary,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-c:a",
        "libmp3lame",
        "-b:a",
        str(bitrate),
        "-write_xing",
        "1",
        str(destination),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_sec)),
            check=False,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 44:
        destination.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout or "FFmpeg failed").strip()
        raise RuntimeError(detail[-1200:])
    return destination
