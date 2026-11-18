from __future__ import annotations

import logging
import threading
import time

import sounddevice as sd

logger = logging.getLogger(__name__)

_REFRESH_LOCK = threading.Lock()


def refresh_audio_devices() -> bool:
    """PortAudio を再初期化し、OS 側のデバイス追加/削除を再検出させる。"""
    terminate = getattr(sd, "_terminate", None)
    initialize = getattr(sd, "_initialize", None)
    if not (callable(terminate) and callable(initialize)):
        logger.warning("PortAudio device refresh is unavailable")
        return False

    with _REFRESH_LOCK:
        try:
            logger.info("Refreshing PortAudio devices")
            terminate()
            time.sleep(0.2)
            initialize()
            return True
        except Exception as e:
            logger.warning("PortAudio device refresh failed: %s", e)
            return False


def _default_input_device_index() -> int | None:
    try:
        dev = sd.default.device
    except Exception:
        return None
    try:
        candidate = dev[0]
    except Exception:
        candidate = dev
    if candidate is None:
        return None
    try:
        idx = int(candidate)
    except Exception:
        return None
    return idx if idx >= 0 else None


def list_input_devices(*, refresh: bool = False) -> list[dict]:
    """マイク入力デバイスの一覧を返す"""
    if refresh:
        refresh_audio_devices()

    devices = sd.query_devices()
    default_input = _default_input_device_index()
    result = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            result.append({
                "id": i,
                "name": d["name"],
                "channels": d["max_input_channels"],
                "sample_rate": int(d["default_samplerate"]),
                "is_default": i == default_input,
                "is_blackhole": "blackhole" in d["name"].lower(),
            })
    return result
