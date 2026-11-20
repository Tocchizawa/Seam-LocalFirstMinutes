"""Bridge the native Apple Speech recognizer into the local Python API."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict], Awaitable[None]]


def _executable() -> str:
    configured = os.environ.get("SEAM_SPEECH_TRANSCRIBE_BIN")
    resources = os.environ.get("SEAM_RESOURCES_DIR")
    candidates = [Path(configured)] if configured else []
    if resources:
        candidates.append(Path(resources) / "apple-speech-transcriber")
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / "macos" / ".build" / "debug" / "AppleSpeechTranscriber")
    candidates.append(repo_root / "macos" / ".build" / "release" / "AppleSpeechTranscriber")
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    path = shutil.which("AppleSpeechTranscriber")
    if path:
        return path
    raise FileNotFoundError(
        "Apple Speech helper not found. Build/package AppleSpeechTranscriber and set "
        "SEAM_SPEECH_TRANSCRIBE_BIN."
    )


class AppleSpeechProcess:
    """One live speech-analysis subprocess that emits finalized segments as it listens."""

    def __init__(self, process: asyncio.subprocess.Process, stop_file: Path,
                 on_event: EventHandler) -> None:
        self.process = process
        self.stop_file = stop_file
        self.on_event = on_event
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        while line := await self.process.stdout.readline():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    await self.on_event(event)
            except json.JSONDecodeError:
                logger.warning("Apple Speech helper emitted invalid JSON: %r", line[:300])
            except Exception:
                logger.exception("Apple Speech event handler failed")

    async def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while line := await self.process.stderr.readline():
            logger.info("[apple-speech] %s", line.decode("utf-8", errors="replace").rstrip())

    async def finish(self) -> None:
        self.stop_file.touch(exist_ok=True)
        await self.wait_file()

    async def wait_file(self) -> None:
        await self.process.wait()
        await asyncio.gather(self._stdout_task, self._stderr_task)
        self.stop_file.unlink(missing_ok=True)
        if self.process.returncode:
            raise RuntimeError(f"Apple Speech helper exited with status {self.process.returncode}")

    async def cancel(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        await asyncio.gather(self._stdout_task, self._stderr_task, return_exceptions=True)
        self.stop_file.unlink(missing_ok=True)


async def start_live_transcription(
    wav_path: Path, session_id: str, language: str, on_event: EventHandler,
) -> AppleSpeechProcess:
    stop_file = wav_path.parent / ".apple-speech-stop"
    stop_file.unlink(missing_ok=True)
    process = await asyncio.create_subprocess_exec(
        _executable(), "--wav", str(wav_path), "--follow", "--stop-file", str(stop_file),
        "--language", language, "--session-id", session_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return AppleSpeechProcess(process, stop_file, on_event)


async def start_file_transcription(
    wav_path: Path, language: str, on_event: EventHandler, *, session_id: str | None = None,
) -> AppleSpeechProcess:
    args = [_executable(), "--wav", str(wav_path), "--language", language]
    if session_id:
        args.extend(["--session-id", session_id])
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return AppleSpeechProcess(process, wav_path.parent / ".apple-speech-unused-stop", on_event)
