from __future__ import annotations

from fastapi import APIRouter

from src.audio.devices import list_input_devices
from src.audio.recorder import recorder

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("")
async def get_devices(refresh: bool = False) -> dict:
    refresh_devices = bool(refresh) and not recorder.is_recording
    system_audio_available = False
    try:
        from src.audio.system_capture import is_available
        system_audio_available = is_available()
    except Exception:
        pass

    return {
        "devices": list_input_devices(refresh=refresh_devices),
        # 既存UI互換のキー名。実態は Core Audio Tap の利用可否。
        "screen_capture_available": system_audio_available,
        "system_audio_available": system_audio_available,
        "devices_refreshed": refresh_devices,
    }
