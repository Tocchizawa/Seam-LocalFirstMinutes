from __future__ import annotations

from fastapi import APIRouter

from src.audio.devices import list_input_devices
from src.audio.recorder import recorder

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("")
async def get_devices(refresh: bool = False) -> dict:
    refresh_devices = bool(refresh) and not recorder.is_recording
    sck_available = False
    try:
        from src.audio.system_capture import is_available
        sck_available = is_available()
    except Exception:
        pass

    return {
        "devices": list_input_devices(refresh=refresh_devices),
        "screen_capture_available": sck_available,
        "devices_refreshed": refresh_devices,
    }
