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


def test_metadata_failure_keeps_nonempty_raw_segment() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sidecar = root / "audio-capture"
        sidecar.write_text("#!/usr/bin/env sh\n")
        sidecar.chmod(0o755)

        cap = system_capture.SystemAudioCapture()
        cap._sidecar_path = sidecar
        cap._wav_path = root / "system.wav"

        def fail_after_raw_write() -> None:
            assert cap._raw_path is not None
            cap._raw_path.write_bytes(array("f", [0.25] * 16).tobytes())
            raise RuntimeError("metadata failed")

        cap._wait_for_metadata = fail_after_raw_write  # type: ignore[method-assign]
        old_popen = system_capture.subprocess.Popen
        system_capture.subprocess.Popen = lambda *_args, **_kwargs: FakeSidecarProcess()  # type: ignore[assignment]
        try:
            try:
                cap._start_segment(0)
                raised = False
            except RuntimeError:
                raised = True

            assert raised is True
            assert cap._segment_paths == [root / "system.raw"]
            assert cap._segment_paths[0].stat().st_size == 64
        finally:
            cap._stop_active_segment()
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


def test_audio_diagnostics_track_silent_tail_after_audio() -> None:
    old_monotonic = system_capture.time.monotonic
    try:
        ticks = iter([10.0, 25.0])
        system_capture.time.monotonic = lambda: next(ticks, 25.0)  # type: ignore[assignment]

        cap = system_capture.SystemAudioCapture()
        cap._sample_rate = 10
        cap._update_audio_stats(array("f", [0.25] * 10).tobytes())
        cap._update_audio_stats(array("f", [0.0] * 30).tobytes())

        diag = cap.get_diagnostics()

        assert diag["has_bytes"] is True
        assert diag["has_audio"] is True
        assert diag["captured_duration_sec"] == 4.0
        assert diag["last_nonzero_audio_sec"] == 1.0
        assert diag["silent_tail_sec"] == 3.0
    finally:
        system_capture.time.monotonic = old_monotonic  # type: ignore[assignment]


def test_segmented_raw_conversion_preserves_all_segments() -> None:
    with tempfile.TemporaryDirectory() as td:
        raw1 = Path(td) / "system.raw"
        raw2 = Path(td) / "system.part1.raw"
        wav = Path(td) / "system.wav"
        rate = 16000
        raw1.write_bytes(array("f", [0.1] * int(rate * 0.1)).tobytes())
        raw2.write_bytes(array("f", [0.2] * int(rate * 0.1)).tobytes())

        cap = system_capture.SystemAudioCapture()
        cap._raw_path = raw2
        cap._wav_path = wav
        cap._sample_rate = rate
        cap._segment_paths = [raw1, raw2]

        cap._convert_raw_to_wav()

        assert cap.error is None
        assert wav.exists() and wav.stat().st_size > 44
        assert not raw1.exists()
        assert not raw2.exists()
        with wave.open(str(wav), "rb") as wf:
            duration = wf.getnframes() / wf.getframerate()
        assert 0.19 <= duration <= 0.21


def test_silent_tail_restart_keeps_same_capture_path() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._segment_index = 0
    stopped: list[bool] = []
    started: list[tuple[int, float | None]] = []

    cap._stop_active_segment = lambda: stopped.append(True)  # type: ignore[method-assign]
    cap._start_segment = (  # type: ignore[method-assign]
        lambda index, gap_started_at=None: started.append((index, gap_started_at))
    )

    restarted = cap._restart_for_silence(22.0, 5)

    assert restarted is True
    assert stopped == [True]
    assert len(started) == 1
    assert started[0][0] == 1
    assert started[0][1] is not None
    assert cap.restart_count == 1
    assert cap.recovery_reasons == ["silent_tail_sec=22.0"]


def test_stream_stopped_error_requests_immediate_restart() -> None:
    cap = system_capture.SystemAudioCapture()
    process = FakeSidecarProcess()
    process.stderr = [
        "SCREEN_CAPTURE_KIT_AUDIO_ERROR stream stopped "
        "domain=com.apple.ScreenCaptureKit.SCStreamErrorDomain code=-3821 "
        "description=system stopped bytes=24435200 duration=381.800s\n"
    ]
    cap._running = True
    cap._process = process  # type: ignore[assignment]

    cap._drain_sidecar_stderr(process)

    assert cap._pending_restart_reason == "stream_stopped code=-3821"
    assert cap._next_restart_at == 0.0
    assert cap._last_stream_error is not None
    assert "code=-3821" in cap._last_stream_error
    assert cap._watchdog_wakeup.is_set()


def test_late_stream_error_from_old_sidecar_is_logged_without_restarting_new_sidecar() -> None:
    cap = system_capture.SystemAudioCapture()
    old_process = FakeSidecarProcess()
    old_process.stderr = [
        "SCREEN_CAPTURE_KIT_AUDIO_ERROR stream stopped "
        "domain=com.apple.ScreenCaptureKit.SCStreamErrorDomain code=-3821\n"
    ]
    cap._running = True
    cap._process = FakeSidecarProcess()  # type: ignore[assignment]

    cap._drain_sidecar_stderr(old_process)

    assert cap._last_stream_error is not None
    assert "code=-3821" in cap._last_stream_error
    assert cap._pending_restart_reason is None


def test_health_check_detects_byte_stall_using_wall_clock() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._process = FakeSidecarProcess()  # type: ignore[assignment]
    cap._has_bytes = True
    cap._last_byte_at = 10.0
    cap._segment_started_at = 5.0

    reason = cap._health_restart_reason(
        now=15.1,
        min_active_sec=30.0,
        silent_restart_sec=20.0,
        byte_stall_restart_sec=5.0,
        restart_cooldown_sec=10.0,
    )

    assert reason == "byte_stall_sec=5.1"


def test_health_check_detects_initial_zero_byte_stall() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._process = FakeSidecarProcess()  # type: ignore[assignment]
    cap._segment_started_at = 10.0

    reason = cap._health_restart_reason(
        now=15.1,
        min_active_sec=30.0,
        silent_restart_sec=20.0,
        byte_stall_restart_sec=5.0,
        restart_cooldown_sec=10.0,
    )

    assert reason == "byte_stall_sec=5.1"


def test_health_check_does_not_repeat_silent_restart_before_new_audio() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._process = FakeSidecarProcess()  # type: ignore[assignment]
    cap._has_bytes = True
    cap._has_nonzero_audio = True
    cap._segment_has_nonzero_audio = False
    cap._sample_rate = 10
    cap._raw_bytes_seen = 400 * 4
    cap._last_nonzero_audio_offset = 100 * 4
    cap._last_byte_at = 100.0
    cap._segment_started_at = 90.0

    reason = cap._health_restart_reason(
        now=100.0,
        min_active_sec=30.0,
        silent_restart_sec=20.0,
        byte_stall_restart_sec=5.0,
        restart_cooldown_sec=10.0,
    )

    assert reason is None


def test_health_check_detects_sidecar_exit_without_audio_growth() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._process = FakeSidecarProcess(poll_result=75)  # type: ignore[assignment]

    reason = cap._health_restart_reason(
        now=1.0,
        min_active_sec=30.0,
        silent_restart_sec=20.0,
        byte_stall_restart_sec=5.0,
        restart_cooldown_sec=10.0,
    )

    assert reason == "sidecar_exit_code=75"


def test_health_check_does_not_restart_after_user_stop() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = False
    cap._pending_restart_reason = "stream_stopped code=-3821"
    cap._process = FakeSidecarProcess(poll_result=75)  # type: ignore[assignment]

    reason = cap._health_restart_reason(
        now=100.0,
        min_active_sec=30.0,
        silent_restart_sec=20.0,
        byte_stall_restart_sec=5.0,
        restart_cooldown_sec=10.0,
    )

    assert reason is None


def test_stream_stop_restart_preserves_gap_from_last_byte() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._segment_index = 0
    cap._last_byte_at = 50.0
    stopped: list[bool] = []
    started: list[tuple[int, float | None]] = []

    cap._stop_active_segment = lambda: stopped.append(True)  # type: ignore[method-assign]
    cap._start_segment = (  # type: ignore[method-assign]
        lambda index, gap_started_at=None: started.append((index, gap_started_at))
    )

    restarted = cap._restart_capture("stream_stopped code=-3821", 5, 0.5)

    assert restarted is True
    assert stopped == [True]
    assert started == [(1, 50.0)]
    assert cap.restart_count == 1
    assert cap.recovery_reasons == ["stream_stopped code=-3821"]


def test_failed_restart_is_scheduled_with_short_backoff() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._segment_index = 0
    stop_count = 0

    def fake_stop() -> None:
        nonlocal stop_count
        stop_count += 1

    cap._stop_active_segment = fake_stop  # type: ignore[method-assign]
    cap._start_segment = (  # type: ignore[method-assign]
        lambda _index, gap_started_at=None: (_ for _ in ()).throw(RuntimeError("start failed"))
    )

    restarted = cap._restart_capture("stream_stopped code=-3821", 5, 0.5)

    assert restarted is False
    assert stop_count == 2
    assert cap.restart_count == 0
    assert cap._pending_restart_reason == "stream_stopped code=-3821"
    assert cap._next_restart_at > 0
    assert cap._recovery_exhausted is False
    assert "start failed" in (cap.error or "")


def test_failed_restarts_wait_for_display_without_consuming_success_budget() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._start_segment = (  # type: ignore[method-assign]
        lambda _index, gap_started_at=None: (_ for _ in ()).throw(RuntimeError("no display"))
    )
    cap._stop_active_segment = lambda: None  # type: ignore[method-assign]

    for _ in range(8):
        assert cap._restart_capture("stream_stopped code=-3815", 5, 0.5) is False

    assert cap.restart_count == 0
    assert cap._restart_failure_count == 8
    assert cap._pending_restart_reason == "stream_stopped code=-3815"
    assert cap._recovery_exhausted is False


def test_restart_succeeds_after_display_returns() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    attempts = 0

    def start_after_wake(_index: int, gap_started_at=None) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("no display")

    cap._start_segment = start_after_wake  # type: ignore[method-assign]
    cap._stop_active_segment = lambda: None  # type: ignore[method-assign]

    assert cap._restart_capture("stream_stopped code=-3815", 5, 0.5) is False
    assert cap._restart_capture("stream_stopped code=-3815", 5, 0.5) is False
    assert cap._restart_capture("stream_stopped code=-3815", 5, 0.5) is True

    assert cap.restart_count == 1
    assert cap.recovery_reasons == ["stream_stopped code=-3815"]
    assert cap._restart_failure_count == 0
    assert cap._pending_restart_reason is None
    assert cap.error is None


def test_non_display_restart_failures_exhaust_after_limit() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._start_segment = (  # type: ignore[method-assign]
        lambda _index, gap_started_at=None: (_ for _ in ()).throw(RuntimeError("permission denied"))
    )
    cap._stop_active_segment = lambda: None  # type: ignore[method-assign]

    for _ in range(5):
        assert cap._restart_capture("sidecar_exit_code=1", 5, 0.5) is False

    assert cap.restart_count == 0
    assert cap._restart_failure_count == 5
    assert cap._pending_restart_reason is None
    assert cap._recovery_exhausted is True


def test_successful_restarts_do_not_exhaust_future_recovery() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._restart_count = 5
    cap._segment_index = 5
    cap._stop_active_segment = lambda: None  # type: ignore[method-assign]
    cap._start_segment = lambda _index, gap_started_at=None: None  # type: ignore[method-assign]

    assert cap._restart_capture("stream_stopped code=-3821", 5, 0.5) is True
    assert cap.restart_count == 6
    assert cap._recovery_exhausted is False


def test_watchdog_restarts_immediately_for_pending_stream_error() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._process = FakeSidecarProcess()  # type: ignore[assignment]
    cap._pending_restart_reason = "stream_stopped code=-3821"
    calls: list[tuple[str, int, float]] = []

    cap._watchdog_settings = lambda: (True, 30.0, 20.0, 5.0, 10.0, 0.5, 5)  # type: ignore[method-assign]

    def fake_restart(reason: str, max_restarts: int, backoff: float) -> bool:
        calls.append((reason, max_restarts, backoff))
        cap._watchdog_stop.set()
        return True

    cap._restart_capture = fake_restart  # type: ignore[method-assign]
    cap._watchdog_wakeup.set()

    cap._watchdog_loop()

    assert calls == [("stream_stopped code=-3821", 5, 0.5)]


def test_watchdog_does_not_exhaust_restart_budget_while_healthy() -> None:
    cap = system_capture.SystemAudioCapture()
    cap._running = True
    cap._process = FakeSidecarProcess()  # type: ignore[assignment]
    cap._restart_count = 5
    cap._watchdog_settings = lambda: (True, 30.0, 20.0, 5.0, 10.0, 0.5, 5)  # type: ignore[method-assign]

    thread = system_capture.threading.Thread(target=cap._watchdog_loop)
    thread.start()
    system_capture.time.sleep(0.35)
    cap._watchdog_stop.set()
    cap._watchdog_wakeup.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert cap._recovery_exhausted is False
    assert cap.error is None


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
        test_metadata_failure_keeps_nonempty_raw_segment,
        test_ffmpeg_conversion_failure_keeps_raw_audio,
        test_placeholder_sidecar_is_not_available,
        test_raw_conversion_creates_wav_and_removes_intermediates,
        test_audio_diagnostics_track_silent_tail_after_audio,
        test_segmented_raw_conversion_preserves_all_segments,
        test_silent_tail_restart_keeps_same_capture_path,
        test_stream_stopped_error_requests_immediate_restart,
        test_late_stream_error_from_old_sidecar_is_logged_without_restarting_new_sidecar,
        test_health_check_detects_byte_stall_using_wall_clock,
        test_health_check_detects_initial_zero_byte_stall,
        test_health_check_does_not_repeat_silent_restart_before_new_audio,
        test_health_check_detects_sidecar_exit_without_audio_growth,
        test_health_check_does_not_restart_after_user_stop,
        test_stream_stop_restart_preserves_gap_from_last_byte,
        test_failed_restart_is_scheduled_with_short_backoff,
        test_failed_restarts_wait_for_display_without_consuming_success_budget,
        test_restart_succeeds_after_display_returns,
        test_non_display_restart_failures_exhaust_after_limit,
        test_successful_restarts_do_not_exhaust_future_recovery,
        test_watchdog_restarts_immediately_for_pending_stream_error,
        test_watchdog_does_not_exhaust_restart_budget_while_healthy,
    ]))
