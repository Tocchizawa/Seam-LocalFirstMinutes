from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from src.api.errors import bad_request, conflict, internal_error, not_found
from src.project.manager import project_manager
from src.security import is_safe_session_id, resolve_existing_absolute_path
from src.storage.db import db
from src.storage.export import export_to_dir, to_markdown
from src.config import APP_DIR, config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/minutes", tags=["minutes"])


def _delete_session_dir(session_id: str) -> tuple[bool, str | None]:
    sid = str(session_id or "").strip()
    if not sid or not is_safe_session_id(sid):
        if sid:
            logger.warning("Skip invalid session_id for delete_session_dir: %s", sid)
        return False, None
    session_dir = APP_DIR / "sessions" / sid
    if not session_dir.exists():
        return False, None
    try:
        shutil.rmtree(session_dir)
        logger.info("Deleted session dir: %s", session_dir)
        return True, None
    except Exception as e:
        logger.warning("Failed to delete session dir %s: %s", session_dir, e)
        return False, str(e)


@router.get("")
async def list_minutes(
    project: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    # 一覧では transcript を返さないので label 付け直しは不要 (高速化)
    return db.list_minutes(project_id=project, limit=limit, offset=offset)


@router.get("/search")
async def search_minutes(
    q: str = Query(..., min_length=1),
    project: str | None = Query(None),
) -> list[dict]:
    # 検索結果も snippet 用 highlights だけ使うので label 付け直し不要
    return db.search(q, project_id=project)


@router.get("/{minutes_id}")
async def get_minutes(minutes_id: str) -> dict:
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    return m


@router.get("/{minutes_id}/transcript")
async def get_transcript(minutes_id: str) -> dict:
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    return {"transcript": m.get("transcript", [])}


@router.get("/{minutes_id}/markdown")
async def get_markdown(minutes_id: str) -> dict:
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    project = project_manager.get(m["project_id"])
    project_name = project.name if project else None
    return {"content": to_markdown(m, project_name)}


@router.post("/{minutes_id}/export")
async def export_minutes(minutes_id: str) -> dict:
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    project = project_manager.get(m["project_id"])
    if project is None:
        raise not_found("PROJECT_NOT_FOUND",
                        f"プロジェクト '{m['project_id']}' が見つかりません")
    path = export_to_dir(m, project.output_dir, project.name)
    return {"status": "exported", "path": str(path)}


class MinutesUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)


@router.patch("/{minutes_id}")
async def update_minutes(minutes_id: str, body: MinutesUpdateRequest) -> dict:
    if body.title is None:
        raise bad_request("NO_FIELDS", "更新項目がありません")
    title = body.title.strip()
    if not title:
        raise bad_request("EMPTY_TITLE", "タイトルは1文字以上必要です")
    ok = db.update_title(minutes_id, title)
    if not ok:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    updated = db.get_minutes(minutes_id)
    if updated is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    return updated


class SummaryUpdateRequest(BaseModel):
    summary: str = Field(max_length=200_000)


class ImportAudioRequest(BaseModel):
    project_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=200)
    start_retranscribe: bool | None = None


_IMPORT_AUDIO_EXTS = {
    ".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".mp4",
    ".ogg", ".opus", ".wav", ".webm", ".mov",
}


def _new_import_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"import_{stamp}_{uuid.uuid4().hex[:6]}"


def _import_source_label(path: Path, audio_path: Path) -> str:
    if path.is_dir():
        return path.name
    return audio_path.stem or audio_path.name


def _parse_import_started_at(source_name: str) -> tuple[str, str]:
    match = re.search(r"(\d{8})[_-]?(\d{6})", source_name)
    if match:
        raw = "".join(match.groups())
        try:
            # naive datetime はローカルタイムとして astimezone される。
            local_dt = datetime.strptime(raw, "%Y%m%d%H%M%S").astimezone()
            return local_dt.isoformat(), local_dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    now = datetime.now(timezone.utc)
    return now.isoformat(), datetime.now().strftime("%Y-%m-%d")


def _pick_import_audio_path(path: Path) -> Path:
    if path.is_file():
        if path.suffix.lower() not in _IMPORT_AUDIO_EXTS:
            raise bad_request("UNSUPPORTED_AUDIO_FILE", "対応していない音声ファイル形式です")
        return path

    if not path.is_dir():
        raise bad_request("IMPORT_PATH_INVALID", "音声ファイルまたはフォルダを選択してください")

    preferred = (
        "combined.flac", "combined.wav", "system.wav", "mic.wav",
        "audio.flac", "audio.wav", "audio.m4a", "audio.mp3",
    )
    for name in preferred:
        cand = path / name
        if cand.exists() and cand.is_file() and cand.stat().st_size > 44:
            return cand

    for cand in sorted(path.iterdir()):
        if cand.is_file() and cand.suffix.lower() in _IMPORT_AUDIO_EXTS and cand.stat().st_size > 44:
            return cand
    raise not_found("AUDIO_NOT_FOUND", "取り込み可能な音声ファイルが見つかりません")


def _load_import_transcript(path: Path) -> list[dict]:
    transcript_path = path / "transcript.jsonl" if path.is_dir() else None
    if transcript_path is None or not transcript_path.exists():
        return []

    segments: list[dict] = []
    try:
        for line in transcript_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict) and item.get("text"):
                segment = {
                    "start": float(item.get("start") or 0),
                    "end": float(item.get("end") or 0),
                    "text": str(item["text"]).strip(),
                }
                if item.get("session_id"):
                    segment["session_id"] = str(item["session_id"])
                segments.append(segment)
    except Exception as e:
        logger.warning("failed to read import transcript %s: %s", transcript_path, e)
        return []
    return segments


def _prepare_import_audio(source_audio: Path, session_dir: Path) -> Path:
    from src.audio.recorder import FFMPEG

    session_dir.mkdir(parents=True, exist_ok=False)
    suffix = source_audio.suffix.lower()

    if suffix in {".flac", ".wav"}:
        dest = session_dir / f"combined{suffix}"
        shutil.copy2(source_audio, dest)
        if dest.stat().st_size <= 44:
            raise bad_request("AUDIO_FILE_EMPTY", "音声ファイルが空です")
        return dest

    dest = session_dir / "combined.flac"
    proc = subprocess.run(
        [
            FFMPEG, "-y",
            "-loglevel", "error",
            "-i", str(source_audio),
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "flac",
            str(dest),
        ],
        capture_output=True,
        timeout=900,
    )
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size <= 44:
        detail = (proc.stderr or b"")[-500:].decode("utf-8", errors="replace")
        raise bad_request("AUDIO_DECODE_FAILED", f"音声ファイルを読み込めませんでした: {detail}")
    return dest


def _enqueue_summary_if_empty(minutes_id: str) -> bool:
    if not bool(config.get("minutes_ai", "auto_generate", default=True)):
        return False
    current = db.get_minutes(minutes_id)
    if current is None or str(current.get("summary") or "").strip():
        return False
    from src.summarize.runner import get_runner

    return get_runner().ensure_queued(minutes_id)


@router.post("/import-audio")
async def import_audio_minutes(body: ImportAudioRequest) -> dict:
    project = project_manager.get(body.project_id)
    if project is None:
        raise not_found("PROJECT_NOT_FOUND", f"プロジェクト '{body.project_id}' が見つかりません")

    try:
        source_path = resolve_existing_absolute_path(body.source_path)
    except Exception:
        raise bad_request("IMPORT_PATH_INVALID", "存在する絶対パスを指定してください")

    source_audio = _pick_import_audio_path(source_path)
    source_label = _import_source_label(source_path, source_audio)
    started_at, date_str = _parse_import_started_at(source_label)
    requested_title = (body.title or "").strip()
    title = requested_title or source_label or "取り込み音声"

    session_id = _new_import_session_id()
    session_dir = APP_DIR / "sessions" / session_id

    try:
        stored_audio = _prepare_import_audio(source_audio, session_dir)
        from src.api import recording as rec_mod

        if not rec_mod._write_session_meta(
            session_id,
            project_id=body.project_id,
            started_at=started_at,
            state="importing",
        ):
            raise internal_error(
                "RECOVERY_STATE_WRITE_FAILED",
                "音声取り込みの復元情報を保存できなかったため、処理を開始できません",
            )
        initial_transcript = _load_import_transcript(source_path)

        duration_sec = 0
        try:
            duration_sec = int(rec_mod._audio_duration_sec(stored_audio))
        except Exception:
            duration_sec = 0
        if duration_sec <= 0 and initial_transcript:
            try:
                duration_sec = int(max(float(seg.get("end", 0.0)) for seg in initial_transcript))
            except Exception:
                duration_sec = 0

        now = datetime.now(timezone.utc).isoformat()
        minutes_id = uuid.uuid4().hex[:12]
        db.insert_minutes({
            "id": minutes_id,
            "session_id": session_id,
            "project_id": body.project_id,
            "title": title,
            "date": date_str,
            "started_at": started_at,
            "duration_sec": int(duration_sec),
            "transcript": initial_transcript,
            "summary": "",
            "llm_model": "",
            "created_at": now,
            "updated_at": now,
        })
    except Exception:
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
        raise

    minutes = db.get_minutes(minutes_id)
    if minutes is None:
        raise RuntimeError("minutes insert failed")

    should_retranscribe = bool(body.start_retranscribe) or not bool(initial_transcript)
    retranscribe = None
    summary_enqueued = False
    if should_retranscribe:
        retranscribe = await _start_retranscribe_minutes(
            minutes_id,
            enqueue_summary_on_complete=True,
        )
    elif initial_transcript:
        try:
            summary_enqueued = _enqueue_summary_if_empty(minutes_id)
            if summary_enqueued:
                logger.info("[import] auto-summary enqueued: %s", minutes_id)
                rec_mod._delete_session_meta(session_id)
            elif not bool(config.get("minutes_ai", "auto_generate", default=True)):
                rec_mod._delete_session_meta(session_id)
            else:
                logger.warning(
                    "[import] retaining recovery metadata until auto-summary "
                    "is durably queued: %s",
                    minutes_id,
                )
        except Exception as e:
            logger.warning("[import] failed to enqueue auto-summary for %s: %s", minutes_id, e)

    return {
        "status": "started",
        "minutes": minutes,
        "session_id": session_id,
        "audio_path": str(stored_audio),
        "source_path": str(source_path),
        "used_existing_transcript": bool(initial_transcript),
        "start_retranscribe": should_retranscribe,
        "summary_enqueued": summary_enqueued,
        "retranscribe": retranscribe,
    }


@router.patch("/{minutes_id}/summary")
async def update_summary(minutes_id: str, body: SummaryUpdateRequest) -> dict:
    """ユーザーによる要約手動編集。LLM モデル列は更新しない(provenance を保持)。"""
    current = db.get_minutes(minutes_id)
    if current is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    session_id = str(current.get("session_id") or "")
    if session_id:
        from src.api import recording as rec_mod

        pipeline = rec_mod._pipelines.get(session_id)
        if pipeline and pipeline.get("state") in ("recording", "stopping", "transcribing"):
            raise conflict("MINUTES_BUSY", "文字起こし中は要約を編集できません")
    from src.summarize.runner import get_runner
    status = get_runner().get_status(minutes_id)
    if status is not None and status.state in ("queued", "running"):
        raise conflict("MINUTES_BUSY", "要約生成中は要約を編集できません")

    ok = db.update_summary(minutes_id, body.summary)
    if not ok:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    updated = db.get_minutes(minutes_id)
    if updated is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    return updated


@router.put("/{minutes_id}/project")
async def reassign_project(minutes_id: str, body: dict) -> dict:
    from src.summarize.runner import get_runner

    if get_runner().is_busy(minutes_id):
        raise conflict("MINUTES_BUSY", "処理中の議事録はプロジェクトを移動できません")
    project_id = body.get("project_id")
    if not project_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail={"code": "MISSING_PROJECT_ID", "message": "project_id は必須です"})
    ok = db.update_project(minutes_id, project_id)
    if not ok:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    return {"status": "updated"}


@router.delete("/{minutes_id}")
async def delete_minutes(minutes_id: str) -> dict:
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    session_id = str(m.get("session_id") or "").strip()
    if session_id:
        from src.api import recording as rec_mod
        existing = rec_mod._pipelines.get(session_id)
        if existing and existing.get("state") in ("recording", "stopping", "transcribing"):
            raise conflict("SESSION_ACTIVE", "この議事録のセッションは現在処理中のため削除できません")

    from src.summarize.runner import get_runner
    if get_runner().is_busy(minutes_id):
        raise conflict("MINUTES_BUSY", "この議事録は要約または後処理中のため削除できません")

    ok = db.delete_minutes(minutes_id)
    if not ok:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")

    session_audio_deleted = False
    audio_cleanup_error: str | None = None
    if session_id:
        session_audio_deleted, audio_cleanup_error = _delete_session_dir(session_id)

    return {
        "status": "deleted",
        "session_id": session_id or None,
        "session_audio_deleted": session_audio_deleted,
        "audio_cleanup_error": audio_cleanup_error,
    }


@router.post("/{minutes_id}/retranscribe")
async def retranscribe_minutes(minutes_id: str) -> dict:
    return await _start_retranscribe_minutes(minutes_id)


async def _start_retranscribe_minutes(
    minutes_id: str,
    *,
    enqueue_summary_on_complete: bool = False,
) -> dict:
    """既存音声を Apple Speech で再認識するバックグラウンド処理を開始する。"""
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")

    session_id = str(m.get("session_id") or "").strip()
    if not session_id:
        raise not_found("SESSION_NOT_FOUND", "セッションIDが見つかりません")

    from src.summarize.runner import get_runner
    if get_runner().is_busy(minutes_id):
        raise conflict("MINUTES_BUSY", "この議事録は要約または後処理中のため再文字起こしできません")

    from src.api import recording as rec_mod
    session_dir = APP_DIR / "sessions" / session_id
    wav_path = next((session_dir / name for name in rec_mod.AUDIO_FILE_PRIORITY
                     if (session_dir / name).is_file() and (session_dir / name).stat().st_size > 44), None)
    if wav_path is None:
        raise not_found("AUDIO_NOT_FOUND", "音声ファイルが見つかりません")

    existing = rec_mod._pipelines.get(session_id)
    if session_id in _retranscribe_tasks or (
        existing and existing.get("state") in ("recording", "stopping", "transcribing")
    ):
        raise conflict("ALREADY_RUNNING", "このセッションは現在処理中です")

    if not rec_mod._write_session_meta(
        session_id,
        project_id=str(m.get("project_id") or ""),
        state="retranscribing",
        retry_minutes_id=minutes_id,
    ):
        raise internal_error("RECOVERY_STATE_WRITE_FAILED", "再文字起こしの復元情報を保存できませんでした")

    cancel_event = asyncio.Event()
    _retranscribe_cancel_events[session_id] = cancel_event
    task = asyncio.create_task(
        _run_retranscribe(
            minutes_id,
            session_id,
            wav_path,
            str(m.get("project_id") or ""),
            cancel_event,
            clear_summary=bool(str(m.get("summary") or "").strip()),
            minutes_title=m.get("title"),
            enqueue_summary_on_complete=enqueue_summary_on_complete or bool(
                config.get("minutes_ai", "auto_generate", default=True)
            ),
        )
    )
    _retranscribe_tasks[session_id] = task
    task.add_done_callback(lambda done, sid=session_id: _retranscribe_tasks.pop(sid, None))
    return {"status": "started", "session_id": session_id}


_retranscribe_sem = asyncio.Semaphore(1)
_retranscribe_tasks: dict[str, asyncio.Task] = {}
_retranscribe_processes: dict[str, object] = {}
_retranscribe_cancel_events: dict[str, asyncio.Event] = {}


@router.post("/{minutes_id}/retranscribe/cancel")
async def cancel_retranscribe_minutes(minutes_id: str) -> dict:
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    session_id = str(m.get("session_id") or "").strip()
    if not session_id:
        raise not_found("SESSION_NOT_FOUND", "セッションIDが見つかりません")

    cancel_event = _retranscribe_cancel_events.get(session_id)
    task = _retranscribe_tasks.get(session_id)
    if cancel_event is None or task is None or task.done():
        return {"status": "not_running", "session_id": session_id}
    cancel_event.set()
    process = _retranscribe_processes.get(session_id)
    if process is not None:
        await process.cancel()
    from src.api import recording as rec_mod
    await rec_mod._broadcast_pipeline(session_id)
    return {"status": "cancelling", "session_id": session_id}


def _normalize_audio_for_speech(source: Path, destination: Path) -> None:
    from src.audio.recorder import FFMPEG
    proc = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(source),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(destination)],
        capture_output=True,
        timeout=900,
    )
    if proc.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 44:
        detail = (proc.stderr or b"")[-500:].decode("utf-8", errors="replace")
        raise RuntimeError(f"音声を Apple Speech 用に変換できませんでした: {detail}")


async def _run_retranscribe(
    minutes_id: str,
    session_id: str,
    audio_path: Path,
    project_id: str,
    cancel_event: asyncio.Event,
    *,
    clear_summary: bool,
    minutes_title: str | None = None,
    enqueue_summary_on_complete: bool = False,
) -> None:
    from src.api import recording as rec_mod
    from src.api.ws import ws_manager
    from src.pipeline.state import Stage, STAGE_LABELS, RETRANSCRIBE_PIPELINE
    from src.transcribe.apple_speech import start_file_transcription

    previous = rec_mod._pipelines.get(session_id) or {}
    rec_mod._pipelines[session_id] = {
        "session_id": session_id,
        "minutes_id": minutes_id,
        "minutes_title": minutes_title,
        "project_id": project_id,
        "state": "transcribing",
        "stage": Stage.QUEUED.value,
        "stage_label": STAGE_LABELS[Stage.QUEUED],
        "stage_step": 1,
        "stage_total": len(RETRANSCRIBE_PIPELINE),
        "message": "文字起こしを準備中...",
        "result": previous.get("result"),
        "transcript": None,
        "error": None,
        "started_at": previous.get("started_at") or datetime.now(timezone.utc).isoformat(),
    }
    rec_mod._evict_completed_pipelines()
    await rec_mod._broadcast_pipeline(session_id)

    def set_stage(stage: Stage, message: str | None = None) -> None:
        idx = RETRANSCRIBE_PIPELINE.index(stage) if stage in RETRANSCRIBE_PIPELINE else 0
        rec_mod._pipelines[session_id].update({
            "stage": stage.value,
            "stage_label": STAGE_LABELS[stage],
            "stage_step": idx + 1,
            "stage_total": len(RETRANSCRIBE_PIPELINE),
            "message": message or STAGE_LABELS[stage],
        })

    segments: list[dict] = []
    process = None
    try:
        async with _retranscribe_sem:
            while rec_mod._active_session_id is not None:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                set_stage(Stage.QUEUED, "録音が終わるまで待機中...")
                await rec_mod._broadcast_pipeline(session_id)
                await asyncio.sleep(1)

            if cancel_event.is_set():
                raise asyncio.CancelledError
            set_stage(Stage.APPLE_SPEECH, "音声を Apple Speech 用に準備中...")
            rec_mod._pipelines[session_id]["progress"] = 0.0
            await rec_mod._broadcast_pipeline(session_id)

            with tempfile.TemporaryDirectory(prefix="seam-apple-speech-") as temp_dir:
                normalized = Path(temp_dir) / "audio.wav"
                await asyncio.to_thread(_normalize_audio_for_speech, audio_path, normalized)
                if cancel_event.is_set():
                    raise asyncio.CancelledError

                emit_speech_event = rec_mod._make_emit_speech_event(
                    session_id, persist=False, broadcast_errors=False,
                )

                async def on_event(event: dict) -> None:
                    if event.get("type") == "transcript_chunk":
                        data = event.get("data") or {}
                        text = str(data.get("text") or "").strip()
                        if text:
                            segments.append({
                                "start": float(data.get("start") or 0),
                                "end": float(data.get("end") or 0),
                                "text": text,
                                "session_id": session_id,
                            })
                    await emit_speech_event(event)

                language = str(config.get("speech", "language", default="ja") or "ja")
                process = await start_file_transcription(
                    normalized,
                    language,
                    on_event,
                    session_id=session_id,
                )
                _retranscribe_processes[session_id] = process
                try:
                    await process.wait_file()
                except RuntimeError:
                    if cancel_event.is_set():
                        raise asyncio.CancelledError
                    raise
                _retranscribe_processes.pop(session_id, None)
                if cancel_event.is_set():
                    raise asyncio.CancelledError

            set_stage(Stage.SAVING, "文字起こしを保存中...")
            rec_mod._pipelines[session_id]["progress"] = 0.95
            await rec_mod._broadcast_pipeline(session_id)
            if not db.update_transcript(minutes_id, segments, clear_summary=clear_summary):
                raise RuntimeError("議事録が削除されたため、文字起こし結果を保存できませんでした")
            transcript_path = audio_path.parent / rec_mod.TRANSCRIPT_JSONL_FILENAME
            transcript_path.write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in segments),
                encoding="utf-8",
            )
            rec_mod._write_session_meta(session_id, state="retry_saved", retry_minutes_id=minutes_id)

            payload = rec_mod.build_pipeline_transcript_payload(segments)
            set_stage(Stage.DONE, "Apple Speech の文字起こしが完了しました")
            rec_mod._pipelines[session_id].update({"state": "done", "progress": 1.0, "transcript": payload})
            rec_mod._evict_completed_pipelines()
            await ws_manager.broadcast({
                "type": "pipeline_done",
                "data": {"session_id": session_id, "minutes_id": minutes_id, "retranscribe": True,
                         "count": len(segments)},
            })
            await rec_mod._broadcast_pipeline(session_id)

            summary_ready = True
            if enqueue_summary_on_complete and bool(config.get("minutes_ai", "auto_generate", default=True)):
                current = db.get_minutes(minutes_id)
                if current is None:
                    summary_ready = False
                elif not str(current.get("summary") or "").strip():
                    summary_ready = _enqueue_summary_if_empty(minutes_id)
            if summary_ready:
                rec_mod._delete_session_meta(session_id)
    except asyncio.CancelledError:
        if process is not None and process.process.returncode is None:
            await process.cancel()
        rec_mod._pipelines[session_id].update({
            "state": "done", "stage": Stage.CANCELLED.value,
            "stage_label": STAGE_LABELS[Stage.CANCELLED], "message": "再文字起こしを停止しました",
        })
        rec_mod._delete_session_meta(session_id)
        await ws_manager.broadcast({
            "type": "pipeline_done",
            "data": {"session_id": session_id, "minutes_id": minutes_id,
                     "retranscribe": True, "cancelled": True},
        })
        await rec_mod._broadcast_pipeline(session_id)
    except Exception as e:
        _retranscribe_processes.pop(session_id, None)
        logger.exception("Apple Speech retranscription failed [%s]", session_id)
        rec_mod._pipelines[session_id].update({"state": "error", "message": str(e), "error": str(e)})
        await ws_manager.broadcast({"type": "pipeline_error", "data": {"session_id": session_id, "message": str(e)}})
        await rec_mod._broadcast_pipeline(session_id)
    finally:
        _retranscribe_processes.pop(session_id, None)
        _retranscribe_cancel_events.pop(session_id, None)
