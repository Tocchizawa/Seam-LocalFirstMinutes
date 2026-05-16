from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.api.errors import bad_request, not_found
from src.api.ws import ws_manager
from src.speakers import pyannote_runner, speaker_memory
from src.storage.db import db

router = APIRouter(prefix="/api/speakers", tags=["speakers"])


class SpeakerRenameRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class SpeakerMergeRequest(BaseModel):
    primary_id: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1, max_length=20)


class HFTokenRequest(BaseModel):
    token: str = Field(min_length=4, max_length=200)


@router.get("/diarization/status")
async def diarization_status() -> dict:
    return {
        "pyannote_available": pyannote_runner.is_available(),
        "has_hf_token": pyannote_runner.has_hf_token(),
    }


@router.put("/diarization/token")
async def set_hf_token(body: HFTokenRequest) -> dict:
    token = body.token.strip()
    if not token:
        raise bad_request("EMPTY_TOKEN", "トークンが空です")
    pyannote_runner.set_hf_token(token)
    return {"status": "saved"}


@router.delete("/diarization/token")
async def delete_hf_token() -> dict:
    pyannote_runner.delete_hf_token()
    return {"status": "deleted"}


@router.post("/diarization/test")
async def test_diarization() -> dict:
    """pyannote パイプラインのロードを試して、エラー詳細を返す。"""
    if not pyannote_runner.has_hf_token():
        return {
            "ok": False,
            "code": "NO_TOKEN",
            "message": "HF トークンが未設定です",
        }
    try:
        # キャッシュをリセットしてからロードを試す (規約承認後の再試行を確実に反映)
        pyannote_runner.reset_pipeline()
        pipeline = pyannote_runner._get_pipeline()
        if pipeline is None:
            return {
                "ok": False,
                "code": "LOAD_FAILED",
                "message": "パイプラインがロードできませんでした",
            }
        return {"ok": True, "message": "pyannote モデルのロードに成功しました"}
    except Exception as e:
        msg = str(e)
        code = "LOAD_FAILED"
        if "gated" in msg.lower() or "403" in msg or "restricted" in msg.lower():
            code = "GATED_REPO"
        elif "401" in msg or "unauthorized" in msg.lower() or "invalid" in msg.lower():
            code = "INVALID_TOKEN"
        return {"ok": False, "code": code, "message": msg[:600]}


@router.get("")
async def list_speakers() -> dict:
    return {"speakers": speaker_memory.list_profiles()}


@router.patch("/{speaker_id}")
async def rename_speaker(speaker_id: str, body: SpeakerRenameRequest) -> dict:
    renamed = speaker_memory.rename(speaker_id, body.label)
    if renamed is None:
        raise not_found("SPEAKER_NOT_FOUND", f"speaker '{speaker_id}' が見つかりません")
    await ws_manager.broadcast(
        {
            "type": "speaker_renamed",
            "data": {
                "speaker_id": renamed["id"],
                "speaker_label": renamed["label"],
                "updated_at": renamed["updated_at"],
            },
        }
    )
    return {"speaker": renamed}


@router.get("/{speaker_id}/sample")
async def get_speaker_sample(speaker_id: str) -> FileResponse:
    sample = speaker_memory.get_sample_path(speaker_id)
    if sample is None:
        raise bad_request("SPEAKER_SAMPLE_NOT_FOUND", "この話者には音声サンプルがありません")
    return FileResponse(str(sample), media_type="audio/wav")


@router.delete("/{speaker_id}")
async def delete_speaker(speaker_id: str) -> dict:
    """話者プロファイルを削除し、DB 内のセグメント参照は無ラベル化する。"""
    removed = speaker_memory.delete(speaker_id)
    if removed is None:
        raise not_found("SPEAKER_NOT_FOUND", f"speaker '{speaker_id}' が見つかりません")
    affected = db.reassign_speaker_id(speaker_id, target_id=None)
    await ws_manager.broadcast(
        {
            "type": "speaker_deleted",
            "data": {"speaker_id": speaker_id, "affected_segments": affected},
        }
    )
    return {"status": "deleted", "speaker_id": speaker_id, "affected_segments": affected}


@router.post("/merge")
async def merge_speakers(body: SpeakerMergeRequest) -> dict:
    """source_ids の話者を primary_id に統合する。

    - speakers.yaml: source プロファイルを削除し、embedding を重み付け平均で primary に統合
    - 議事録 DB: source の speaker_id を持つセグメントを全て primary の id/label に書き換え
    """
    # primary 自身が source に含まれている場合は除外
    sources = [sid for sid in body.source_ids if sid and sid != body.primary_id]
    if not sources:
        raise bad_request("NO_SOURCES", "統合元の話者 ID が指定されていません")

    result = speaker_memory.merge(body.primary_id, sources)
    if result is None:
        raise not_found("SPEAKER_NOT_FOUND", f"primary '{body.primary_id}' が見つかりません")

    primary = result["primary"]
    primary_label = str(primary.get("label") or "")
    total_affected = 0
    for src in sources:
        total_affected += db.reassign_speaker_id(
            src, target_id=body.primary_id, target_label=primary_label,
        )

    await ws_manager.broadcast(
        {
            "type": "speakers_merged",
            "data": {
                "primary_id": body.primary_id,
                "merged_ids": sources,
                "affected_segments": total_affected,
            },
        }
    )
    return {
        "status": "merged",
        "primary": primary,
        "merged_ids": sources,
        "affected_segments": total_affected,
    }
