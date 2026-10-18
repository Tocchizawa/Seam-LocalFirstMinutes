from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict

from src.config import config
from src.live_notes.coordinator import get_coordinator
from src.live_notes.models import DocumentEditsRequest, HumanOperation, resolve_live_provider
from src.live_notes.repository import NoteError

router = APIRouter(prefix="/api/live-notes", tags=["live-notes"])


async def execute(method, *args, **kwargs):
    try:
        return await method(*args, **kwargs)
    except NoteError as e:
        raise HTTPException(
            e.status, detail={"code": e.code, "message": str(e), "latest": e.latest}
        ) from e


@router.get("")
async def sessions():
    return await get_coordinator().call("sessions")


@router.get("/preflight")
async def preflight(provider: Literal["inherit", "codex", "claude_code"] = "inherit"):
    from src.live_notes.providers import create_provider

    name = (
        provider
        if provider != "inherit"
        else config.get("minutes_ai", "provider", default="")
    )
    if name not in ("codex", "claude_code"):
        return {
            "ok": False,
            "error": "Codex CLIまたはClaude Code CLIを選択してください",
        }
    try:
        _, provider_config = resolve_live_provider(
            {**(config.get("live_notes") or {}), "provider": name},
            config.get("minutes_ai") or {},
        )
        return await create_provider(name, provider_config).preflight()
    except Exception:
        return {
            "ok": False,
            "error": "CLIを確認できません。バイナリまたはランチャー設定を確認してください",
        }


@router.get("/{session_id}")
async def snapshot(session_id: str):
    return await execute(get_coordinator().call, "snapshot", session_id)


@router.get("/{session_id}/export.md", response_class=PlainTextResponse)
async def export(session_id: str):
    return await execute(get_coordinator().call, "markdown", session_id)


@router.get("/{session_id}/history")
async def history(
    session_id: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
):
    return await execute(get_coordinator().call, "history", session_id, limit, offset)


@router.get("/{session_id}/source/{segment_seq}")
async def source(session_id: str, segment_seq: int):
    return await execute(get_coordinator().call, "source", session_id, segment_seq)


@router.post("/{session_id}/document/edits")
async def document_edits(session_id: str, body: DocumentEditsRequest):
    return await execute(get_coordinator().call, "document_edits", session_id, body)


@router.post("/{session_id}/topics")
async def add_topic(session_id: str, body: HumanOperation):
    return await execute(get_coordinator().human, session_id, "create_topic", body)


@router.post("/{session_id}/topics/reorder")
async def reorder(session_id: str, body: HumanOperation):
    return await execute(get_coordinator().human, session_id, "reorder", body)


@router.patch("/{session_id}/topics/{topic_id}")
async def edit_topic(session_id: str, topic_id: str, body: HumanOperation):
    return await execute(
        get_coordinator().human, session_id, "edit_topic", body, topic_id
    )


@router.delete("/{session_id}/topics/{topic_id}")
async def delete_topic(session_id: str, topic_id: str, body: HumanOperation):
    return await execute(
        get_coordinator().human, session_id, "delete_topic", body, topic_id
    )


@router.post("/{session_id}/blocks")
async def add_block(session_id: str, body: HumanOperation):
    return await execute(get_coordinator().human, session_id, "add_block", body)


@router.patch("/{session_id}/blocks/{block_id}")
async def edit_block(session_id: str, block_id: str, body: HumanOperation):
    return await execute(
        get_coordinator().human, session_id, "edit_block", body, block_id
    )


@router.delete("/{session_id}/blocks/{block_id}")
async def delete_block(session_id: str, block_id: str, body: HumanOperation):
    return await execute(
        get_coordinator().human, session_id, "delete_block", body, block_id
    )


@router.post("/{session_id}/history/{event_id}/undo")
async def undo(session_id: str, event_id: str, body: HumanOperation):
    return await execute(get_coordinator().human, session_id, "undo", body, event_id)


@router.post("/{session_id}/suggestions/{suggestion_id}/resolve")
async def resolve(session_id: str, suggestion_id: str, body: HumanOperation):
    return await execute(
        get_coordinator().human, session_id, "resolve", body, suggestion_id
    )


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "resume", "flush", "retry"]


@router.post("/{session_id}/control")
async def control(session_id: str, body: Control):
    return await execute(get_coordinator().control, session_id, body.action)
