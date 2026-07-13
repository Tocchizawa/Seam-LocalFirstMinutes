"""System audio capture failure handling tests."""
from __future__ import annotations

import sys
import tempfile
import types
import wave
from array import array
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audio import system_capture


class FakeSidecarProcess:
    def __init__(self, poll_result=None) -> None:
        self.stderr: list[str] = []
        self.terminated = False
        self._poll_result = poll_result

    def poll(self):
        return self._poll_result

    def terminate(self) -> None:
        self.terminated = True
        self._poll_result = 0

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.terminated = True
        self._poll_result = -9


def test_start_capture_timeout_is_reported_as_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        sidecar = Path(td) / "audio-capture"
        sidecar.write_text("#!/usr/bin/env sh\nsleep 30\n")
        sidecar.chmod(0o755)

        old_os_supported = system_capture._screen_capture_kit_os_supported
        old_find_sidecar = system_capture._find_audio_capture_sidecar
        old_popen = system_capture.subprocess.Popen
        old_monotonic = system_capture.time.monotonic
        old_sleep = system_capture.time.sleep
        try:
            fake_process = FakeSidecarProcess()
            system_capture._screen_capture_kit_os_supported = lambda: True
            system_capture._find_audio_capture_sidecar = lambda: sidecar
            system_capture.subprocess.Popen = lambda *_args, **_kwargs: fake_process  # type: ignore[assignment]
            ticks = iter([0.0, 0.0, 11.0])
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
            system_capture._screen_capture_kit_os_supported = old_os_supported
            system_capture._find_audio_capture_sidecar = old_find_sidecar
            system_capture.subprocess.Popen = old_popen  # type: ignore[assignment]
            system_capture.time.monotonic = old_monotonic  # type: ignore[assignment]
            system_capture.time.sleep = old_sleep  # type: ignore[assignment]


def test_screen_capture_kit_aliases_route_to_screencapturekit() -> None:
    values = [
        "sck",
        "screen_capture_kit",
        "screen-capture-kit",
    ]
    for value in values:
        assert system_capture._normalize_capture_method(value) == "screencapturekit"


def test_removed_backends_are_not_normalized_to_screencapturekit() -> None:
    values = [
        "auto",
        "coreaudio_tap",
        "tap",
        "blackhole",
        "legacy_backend",
        "virtual_device",
        "unknown",
    ]
    for value in values:
        assert system_capture._normalize_capture_method(value) == value


def test_removed_backend_start_fails_before_sidecar_launch() -> None:
    old_normalize = system_capture._normalize_capture_method
    old_popen = system_capture.subprocess.Popen
    try:
        for value in ("auto", "coreaudio_tap", "tap", "blackhole"):
            calls: list[bool] = []
            system_capture._normalize_capture_method = lambda _value=None, v=value: v  # type: ignore[assignment]
            system_capture.subprocess.Popen = lambda *_args, **_kwargs: calls.append(True)  # type: ignore[assignment]

            cap = system_capture.SystemAudioCapture()
            try:
                cap.start(Path("/tmp/system.wav"))
                raised = False
            except RuntimeError as e:
                raised = "未対応" in str(e)

            assert raised
            assert calls == []
    finally:
        system_capture._normalize_capture_method = old_normalize
        system_capture.subprocess.Popen = old_popen  # type: ignore[assignment]


def test_start_uses_screencapturekit_sidecar_mode() -> None:
    with tempfile.TemporaryDirectory() as td:
        sidecar = Path(td) / "audio-capture"
        sidecar.write_text("#!/usr/bin/env sh\nsleep 30\n")
        sidecar.chmod(0o755)
        output = Path(td) / "system.wav"
        calls: list[list[str]] = []

        old_os_supported = system_capture._screen_capture_kit_os_supported
        old_find_sidecar = system_capture._find_audio_capture_sidecar
        old_popen = system_capture.subprocess.Popen
        try:
            fake_process = FakeSidecarProcess()

            def fake_popen(cmd, *_args, **_kwargs):
                calls.append(list(cmd))
                meta_path = Path(cmd[cmd.index("--meta-path") + 1])
                meta_path.write_text('{"backend":"screencapturekit","format":"f32le","channels":1,"sample_rate":16000}')
                return fake_process

            system_capture._screen_capture_kit_os_supported = lambda: True
            system_capture._find_audio_capture_sidecar = lambda: sidecar
            system_capture.subprocess.Popen = fake_popen  # type: ignore[assignment]

            cap = system_capture.SystemAudioCapture()
            cap.start(output, sample_rate=16000)
            cap.stop()

            assert calls
            assert "--mode" in calls[0]
            assert calls[0][calls[0].index("--mode") + 1] == "screencapturekit"
            assert cap.backend == "screencapturekit"
        finally:
            system_capture._screen_capture_kit_os_supported = old_os_supported
            system_capture._find_audio_capture_sidecar = old_find_sidecar
            system_capture.subprocess.Popen = old_popen  # type: ignore[assignment]


def test_ffmpeg_conversion_failure_keeps_raw_audio() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_run = system_capture.subprocess.run
        try:
            raw = Path(td) / "system.raw"
            wav = Path(td) / "system.wav"
            raw.write_bytes(b"\x00" * 128)

            def fake_run(*_args, **_kwargs):
                return types.SimpleNamespace(returncode=1, stderr="conversion failed")

            system_capture.subprocess.run = fake_run  # type: ignore[assignment]

            cap = system_capture.SystemAudioCapture()
            cap._running = True
            cap._backend = "screencapturekit"
            cap._raw_path = raw
            cap._wav_path = wav
            cap.stop()

            assert raw.exists()
            assert not wav.exists() or wav.stat().st_size <= 44
            assert "conversion failed" in (cap.error or "")
        finally:
            system_capture.subprocess.run = old_run  # type: ignore[assignment]


def test_placeholder_sidecar_is_not_available() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "audio-capture"
        path.write_text("#!/usr/bin/env sh\nsidecar has not been built\n")
        path.chmod(0o755)

        assert system_capture._is_placeholder_sidecar(path)


def test_raw_conversion_creates_wav_and_removes_intermediates() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "system.raw"
        wav = Path(td) / "system.wav"
        meta = Path(td) / "system.meta.json"
        rate = 16000
        raw.write_bytes(array("f", [0.1] * int(rate * 0.1)).tobytes())
        meta.write_text("{}")

        cap = system_capture.SystemAudioCapture()
        cap._raw_path = raw
        cap._wav_path = wav
        cap._meta_path = meta
        cap._sample_rate = rate

        cap._convert_raw_to_wav()

        assert cap.error is None
        assert wav.exists() and wav.stat().st_size > 44
        assert not raw.exists()
        assert not meta.exists()
        with wave.open(str(wav), "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()
        assert 0.09 <= duration <= 0.11


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
        test_screen_capture_kit_aliases_route_to_screencapturekit,
        test_removed_backends_are_not_normalized_to_screencapturekit,
        test_removed_backend_start_fails_before_sidecar_launch,
        test_start_uses_screencapturekit_sidecar_mode,
        test_ffmpeg_conversion_failure_keeps_raw_audio,
        test_placeholder_sidecar_is_not_available,
        test_raw_conversion_creates_wav_and_removes_intermediates,
    ]))
