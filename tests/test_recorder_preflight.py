"""Recorder mic preflight recovery tests."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audio import recorder as recorder_module
from src.audio.recorder import Recorder


def test_portaudio_internal_error_detection() -> None:
    rec = Recorder()
    assert rec._is_portaudio_internal_error(
        "Internal PortAudio error [PaErrorCode -9986]",
    )
    assert not rec._is_portaudio_internal_error("Invalid device [PaErrorCode -9996]")


def test_preflight_retries_after_portaudio_reset() -> None:
    rec = Recorder()
    attempts: list[int] = []
    resets: list[str] = []

    def probe_with_one_failure(device_idx: int) -> None:
        attempts.append(device_idx)
        if len(attempts) == 1:
            raise RuntimeError(
                "Error opening InputStream: Internal PortAudio error [PaErrorCode -9986]",
            )

    rec._default_input_device_index = lambda: 3  # type: ignore[method-assign]
    rec._mic_internal_error_recovery_settings = lambda: (True, 2, 0.0)  # type: ignore[method-assign]
    rec._reset_audio_backend = lambda reason: resets.append(reason) is None or True  # type: ignore[method-assign]
    rec._probe_input_stream = probe_with_one_failure  # type: ignore[method-assign]

    chosen = rec._prepare_mic_device_for_start(None)
    assert attempts == [3, 3]
    assert len(resets) == 1
    assert chosen == 3


def test_preflight_falls_back_after_non_internal_error() -> None:
    rec = Recorder()
    attempts: list[int] = []

    def probe_requested_then_default(device_idx: int) -> None:
        attempts.append(device_idx)
        if device_idx == 4:
            raise RuntimeError("Error querying device [PaErrorCode -9996]")

    rec._default_input_device_index = lambda: 3  # type: ignore[method-assign]
    rec._mic_internal_error_recovery_settings = lambda: (True, 2, 0.0)  # type: ignore[method-assign]
    rec._reset_audio_backend = lambda reason: False  # type: ignore[method-assign]
    rec._probe_input_stream = probe_requested_then_default  # type: ignore[method-assign]

    chosen = rec._prepare_mic_device_for_start(4)
    assert attempts == [4, 3]
    assert chosen == 3


def test_error_message_separates_permission_from_portaudio_recovery() -> None:
    rec = Recorder()
    msg = rec._build_mic_start_error_message(
        "Error opening InputStream: Internal PortAudio error [PaErrorCode -9986]",
        requested_device=3,
        default_device=3,
        fallback_attempted=False,
    )
    assert "アプリ再起動" in msg
    assert "PortAudio" in msg


def test_system_audio_short_track_is_reported() -> None:
    rec = Recorder()
    rec._capture_system_requested = True
    rec._system_capture_started = True
    rec._system_coverage_settings = lambda: (True, 60.0, 0.85, 20.0)  # type: ignore[method-assign]

    msg = rec._validate_system_audio_coverage(
        mic_duration=1049.2,
        system_duration=240.0,
        elapsed=1049.2,
        sys_ok=True,
    )

    assert msg is not None
    assert "System audio ended early" in msg
    assert "途中" in msg


def test_system_audio_near_full_length_is_ok() -> None:
    rec = Recorder()
    rec._capture_system_requested = True
    rec._system_capture_started = True
    rec._system_coverage_settings = lambda: (True, 60.0, 0.85, 20.0)  # type: ignore[method-assign]

    msg = rec._validate_system_audio_coverage(
        mic_duration=1049.2,
        system_duration=1035.0,
        elapsed=1049.2,
        sys_ok=True,
    )

    assert msg is None


def test_system_audio_full_length_silence_is_reported() -> None:
    rec = Recorder()
    rec._capture_system_requested = True
    rec._system_capture_started = True
    rec._system_coverage_settings = lambda: (True, 60.0, 0.85, 20.0)  # type: ignore[method-assign]

    msg = rec._validate_system_audio_coverage(
        mic_duration=600.0,
        system_duration=600.0,
        elapsed=600.0,
        sys_ok=True,
        system_diagnostics={"has_bytes": True, "has_audio": False},
    )

    assert msg is not None
    assert "only silence" in msg
    assert "全時間で無音" in msg


def test_short_system_audio_silence_is_not_reported() -> None:
    rec = Recorder()
    rec._capture_system_requested = True
    rec._system_capture_started = True
    rec._system_coverage_settings = lambda: (True, 60.0, 0.85, 20.0)  # type: ignore[method-assign]

    msg = rec._validate_system_audio_coverage(
        mic_duration=12.0,
        system_duration=12.0,
        elapsed=12.0,
        sys_ok=True,
        system_diagnostics={"has_bytes": True, "has_audio": False},
    )

    assert msg is None


def test_system_audio_silent_tail_after_audio_is_reported() -> None:
    rec = Recorder()
    rec._capture_system_requested = True
    rec._system_capture_started = True
    rec._system_coverage_settings = lambda: (True, 60.0, 0.85, 20.0)  # type: ignore[method-assign]

    msg = rec._validate_system_audio_coverage(
        mic_duration=600.0,
        system_duration=600.0,
        elapsed=600.0,
        sys_ok=True,
        system_diagnostics={
            "has_bytes": True,
            "has_audio": True,
            "last_nonzero_audio_sec": 120.0,
            "silent_tail_sec": 480.0,
        },
    )

    assert msg is not None
    assert "became silent" in msg
    assert "途中から無音化" in msg


def test_short_system_audio_silent_tail_after_audio_is_not_reported() -> None:
    rec = Recorder()
    rec._capture_system_requested = True
    rec._system_capture_started = True
    rec._system_coverage_settings = lambda: (True, 60.0, 0.85, 20.0)  # type: ignore[method-assign]

    msg = rec._validate_system_audio_coverage(
        mic_duration=600.0,
        system_duration=600.0,
        elapsed=600.0,
        sys_ok=True,
        system_diagnostics={
            "has_bytes": True,
            "has_audio": True,
            "last_nonzero_audio_sec": 565.0,
            "silent_tail_sec": 35.0,
        },
    )

    assert msg is None


def test_system_capture_failure_prevents_mic_only_start() -> None:
    with tempfile.TemporaryDirectory() as td:
        old_sessions_dir = recorder_module.SESSIONS_DIR
        try:
            recorder_module.SESSIONS_DIR = Path(td)
            rec = Recorder()
            mic_started: list[bool] = []

            rec._mic_capture_method = lambda: "coreaudio_sidecar"  # type: ignore[method-assign]
            rec._resolve_mic_device_index_for_sidecar = lambda _device: None  # type: ignore[method-assign]
            rec._start_coreaudio_mic_capture = lambda: mic_started.append(True)  # type: ignore[method-assign]

            def fail_system() -> bool:
                raise RuntimeError("ScreenCaptureKit unavailable")

            rec._start_system_capture = fail_system  # type: ignore[method-assign]

            try:
                rec.start(capture_system=True, session_id="system-failure")
                raised = False
            except RuntimeError as e:
                raised = "内部音声" in str(e)

            assert raised
            assert mic_started == []
            assert not rec.is_recording
        finally:
            recorder_module.SESSIONS_DIR = old_sessions_dir


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
        test_portaudio_internal_error_detection,
        test_preflight_retries_after_portaudio_reset,
        test_preflight_falls_back_after_non_internal_error,
        test_error_message_separates_permission_from_portaudio_recovery,
        test_system_audio_short_track_is_reported,
        test_system_audio_near_full_length_is_ok,
        test_system_audio_full_length_silence_is_reported,
        test_short_system_audio_silence_is_not_reported,
        test_system_audio_silent_tail_after_audio_is_reported,
        test_short_system_audio_silent_tail_after_audio_is_not_reported,
        test_system_capture_failure_prevents_mic_only_start,
    ]))
