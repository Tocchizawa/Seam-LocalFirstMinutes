from __future__ import annotations

from fastapi import APIRouter

from src.audio.devices import list_input_devices

router = APIRouter(prefix="/api/devices", tags=["devices"])


@router.get("")
async def get_devices() -> dict:
    sck_available = False
    try:
        from src.audio.system_capture import is_available
        sck_available = is_available()
    except Exception:
        pass

    return {
        "devices": list_input_devices(),
        "screen_capture_available": sck_available,
    }
