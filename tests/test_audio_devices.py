"""Audio device enumeration tests."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.audio.devices as audio_devices


class _Default:
    def __init__(self, device: object) -> None:
        self.device = device


class _InputOutputPair:
    def __init__(self, input_device: int, output_device: int) -> None:
        self.input_device = input_device
        self.output_device = output_device

    def __getitem__(self, index: int) -> int:
        return [self.input_device, self.output_device][index]


class _FakeSoundDevice:
    def __init__(self, *, fail_refresh: bool = False) -> None:
        self.calls: list[str] = []
        self.default = _Default(_InputOutputPair(1, -1))
        self.fail_refresh = fail_refresh
        self.devices = [
            {
                "name": "Built-in Output",
                "max_input_channels": 0,
                "default_samplerate": 48000,
            },
            {
                "name": "MacBook Pro Microphone",
                "max_input_channels": 1,
                "default_samplerate": 48000,
            },
            {
                "name": "BlackHole 2ch",
                "max_input_channels": 2,
                "default_samplerate": 48000,
            },
        ]

    def _terminate(self) -> None:
        self.calls.append("terminate")
        if self.fail_refresh:
            raise RuntimeError("terminate failed")

    def _initialize(self) -> None:
        self.calls.append("initialize")

    def query_devices(self) -> list[dict]:
        self.calls.append("query")
        return self.devices


def test_refreshes_portaudio_before_listing_devices() -> None:
    original = audio_devices.sd
    fake = _FakeSoundDevice()
    audio_devices.sd = fake  # type: ignore[assignment]
    try:
        devices = audio_devices.list_input_devices(refresh=True)
    finally:
        audio_devices.sd = original  # type: ignore[assignment]

    assert fake.calls == ["terminate", "initialize", "query"]
    assert [d["name"] for d in devices] == ["MacBook Pro Microphone", "BlackHole 2ch"]
    assert devices[0]["is_default"] is True
    assert devices[1]["is_blackhole"] is True


def test_refresh_failure_still_lists_devices() -> None:
    original = audio_devices.sd
    fake = _FakeSoundDevice(fail_refresh=True)
    audio_devices.sd = fake  # type: ignore[assignment]
    try:
        devices = audio_devices.list_input_devices(refresh=True)
    finally:
        audio_devices.sd = original  # type: ignore[assignment]

    assert fake.calls == ["terminate", "query"]
    assert len(devices) == 2


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
        test_refreshes_portaudio_before_listing_devices,
        test_refresh_failure_still_lists_devices,
    ]))
