"""System audio capture failure handling tests."""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audio import system_capture


class FakeConfig:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def setCapturesAudio_(self, _value):
        pass

    def setExcludesCurrentProcessAudio_(self, _value):
        pass

    def setWidth_(self, _value):
        pass

    def setHeight_(self, _value):
        pass

    def setSampleRate_(self, _value):
        pass

    def setChannelCount_(self, _value):
        pass


class FakeFilter:
    @classmethod
    def alloc(cls):
        return cls()

    def initWithDisplay_excludingWindows_(self, _display, _windows):
        return self


class FakeStream:
    @classmethod
    def alloc(cls):
        return cls()

    def initWithFilter_configuration_delegate_(self, _filter, _config, _delegate):
        return self

    def addStreamOutput_type_sampleHandlerQueue_error_(self, *_args):
        return True, None

    def startCaptureWithCompletionHandler_(self, _handler):
        # callback を呼ばず timeout させる。
        return None


class FakeContent:
    def displays(self):
        return ["display"]


class FakeRunningStream:
    def stopCaptureWithCompletionHandler_(self, handler):
        handler(None)


def install_fake_sck() -> tuple[object | None, object, object]:
    old_module = sys.modules.get("ScreenCaptureKit")
    old_content = system_capture._get_shareable_content_sync
    old_create_handler = system_capture._create_handler
    fake = types.ModuleType("ScreenCaptureKit")
    fake.SCStreamConfiguration = FakeConfig
    fake.SCContentFilter = FakeFilter
    fake.SCStream = FakeStream
    sys.modules["ScreenCaptureKit"] = fake
    system_capture._get_shareable_content_sync = lambda: FakeContent()
    system_capture._create_handler = lambda *_args, **_kwargs: object()
    return old_module, old_content, old_create_handler


def restore_fake_sck(old_module: object | None, old_content, old_create_handler) -> None:
    if old_module is None:
        sys.modules.pop("ScreenCaptureKit", None)
    else:
        sys.modules["ScreenCaptureKit"] = old_module
    system_capture._get_shareable_content_sync = old_content
    system_capture._create_handler = old_create_handler


def test_start_capture_timeout_is_reported_as_failure() -> None:
    old_module, old_content, old_create_handler = install_fake_sck()
    try:
        with tempfile.TemporaryDirectory() as td:
            cap = system_capture.SystemAudioCapture()
            try:
                cap.start(Path(td) / "system.wav")
                raised = False
            except RuntimeError as e:
                raised = "タイムアウト" in str(e)
            assert raised
            assert not cap._running
            assert "タイムアウト" in (cap.error or "")
    finally:
        restore_fake_sck(old_module, old_content, old_create_handler)


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
            cap._stream = FakeRunningStream()
            cap._raw_path = raw
            cap._wav_path = wav
            cap._raw_file = open(raw, "ab")
            cap.stop()

            assert raw.exists()
            assert not wav.exists() or wav.stat().st_size <= 44
            assert "conversion failed" in (cap.error or "")
        finally:
            subprocess.run = old_run  # type: ignore[assignment]


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
    ]))
