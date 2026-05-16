from __future__ import annotations

import sounddevice as sd


def list_input_devices() -> list[dict]:
    """マイク入力デバイスの一覧を返す"""
    devices = sd.query_devices()
    result = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            result.append({
                "id": i,
                "name": d["name"],
                "channels": d["max_input_channels"],
                "sample_rate": int(d["default_samplerate"]),
                "is_default": i == sd.default.device[0],
                "is_blackhole": "blackhole" in d["name"].lower(),
            })
    return result
