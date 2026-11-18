from __future__ import annotations

from fastapi import APIRouter

from src.api.errors import bad_request, conflict, not_found
from src.audio.resource_monitor import model_resource_gate
from src.transcribe.streaming import (
    delete_whisper_model,
    get_whisper_model_catalog,
    start_whisper_model_download,
)

router = APIRouter(prefix="/api/models", tags=["models"])


@router.get("/whisper")
async def list_whisper_models() -> dict:
    """Whisperの既知モデルと、現在のダウンロード状態を返す。"""
    return get_whisper_model_catalog()


@router.post("/whisper/{model_name}/download")
async def download_whisper_model(model_name: str) -> dict:
    """WhisperモデルをバックグラウンドでHFキャッシュへダウンロードする。"""
    if model_resource_gate.snapshot().get("whisper_users", 0):
        raise conflict("MODEL_BUSY", "録音中はWhisperモデルをダウンロードできません")
    try:
        download = start_whisper_model_download(model_name)
    except ValueError as exc:
        raise bad_request("UNKNOWN_MODEL", str(exc)) from exc
    except RuntimeError as exc:
        raise conflict("MODEL_DOWNLOAD_BUSY", str(exc)) from exc
    return {"status": "started", "download": download}


@router.delete("/whisper/{model_name}")
async def remove_whisper_model(model_name: str) -> dict:
    """WhisperモデルのHFキャッシュを削除する。"""
    try:
        delete_whisper_model(model_name)
    except ValueError as exc:
        raise bad_request("UNKNOWN_MODEL", str(exc)) from exc
    except RuntimeError as exc:
        raise conflict("MODEL_BUSY", str(exc)) from exc
    except FileNotFoundError as exc:
        raise not_found("MODEL_NOT_DOWNLOADED", "モデルはダウンロードされていません") from exc
    return {"status": "deleted", "model": model_name}
