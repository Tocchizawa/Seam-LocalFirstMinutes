"""System audio capture failure handling tests."""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audio import system_capture


class FakeSidecarProcess:
    def __init__(self) -> None:
        self.stderr: list[str] = []
        self.terminated = False

    def poll(self):
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.terminated = True


def test_start_capture_timeout_is_reported_as_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        sidecar = Path(td) / "audio-capture"
        sidecar.write_text("#!/usr/bin/env sh\nsleep 30\n")
        sidecar.chmod(0o755)

        old_os_supported = system_capture._core_audio_tap_os_supported
        old_find_sidecar = system_capture._find_audio_capture_sidecar
        old_popen = system_capture.subprocess.Popen
        old_monotonic = system_capture.time.monotonic
        old_sleep = system_capture.time.sleep
        try:
            fake_process = FakeSidecarProcess()
            system_capture._core_audio_tap_os_supported = lambda: True
            system_capture._find_audio_capture_sidecar = lambda: sidecar
            system_capture.subprocess.Popen = lambda *_args, **_kwargs: fake_process  # type: ignore[assignment]
            ticks = iter([0.0, 11.0])
            system_capture.time.monotonic = lambda: next(ticks, 11.0)  # type: ignore[assignment]
            system_capture.time.sleep = lambda _seconds: None  # type: ignore[assignment]

            cap = system_capture.SystemAudioCapture()
            try:
                cap.start(Path(td) / "system.wav")
                raised = False
            except RuntimeError as e:
                raised = "タイムアウト" in str(e)
            assert raised
            assert not cap._running
            assert "タイムアウト" in (cap.error or "")
            assert fake_process.terminated
        finally:
            system_capture._core_audio_tap_os_supported = old_os_supported
            system_capture._find_audio_capture_sidecar = old_find_sidecar
            system_capture.subprocess.Popen = old_popen  # type: ignore[assignment]
            system_capture.time.monotonic = old_monotonic  # type: ignore[assignment]
            system_capture.time.sleep = old_sleep  # type: ignore[assignment]


def test_ffmpeg_conversion_failure_keeps_raw_audio() -> None:
    with tempfile.TemporaryDirectory() as td:
        import subprocess

        old_run = subprocess.run
        try:
            raw = Path(td) / "system.raw"
            wav = Path(td) / "system.wav"
            raw.write_bytes(b"\x00" * 128)

            def fake_run(*_args, **_kwargs):
                return types.SimpleNamespace(returncode=1, stderr="conversion failed")

            subprocess.run = fake_run  # type: ignore[assignment]

            cap = system_capture.SystemAudioCapture()
            cap._running = True
            cap._backend = "coreaudio_tap"
            cap._raw_path = raw
            cap._wav_path = wav
            cap.stop()

            assert raw.exists()
            assert not wav.exists() or wav.stat().st_size <= 44
            assert "conversion failed" in (cap.error or "")
        finally:
            subprocess.run = old_run  # type: ignore[assignment]


def test_placeholder_sidecar_is_not_available() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "audio-capture"
        path.write_text("#!/usr/bin/env sh\nsidecar has not been built\n")
        path.chmod(0o755)

        assert system_capture._is_placeholder_sidecar(path)


def _run_as_script(tests: list[Callable[[], None]]) -> int:
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL: {test.__name__}: {e}")
    print("\n=========================================")
    print(f"  結果: {len(tests) - failed} passed, {failed} failed")
    print("=========================================")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_as_script([
        test_start_capture_timeout_is_reported_as_failure,
        test_ffmpeg_conversion_failure_keeps_raw_audio,
        test_placeholder_sidecar_is_not_available,
    ]))
