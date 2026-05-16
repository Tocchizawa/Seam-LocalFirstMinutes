from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from src.api.errors import bad_request, conflict, not_found
from src.project.manager import project_manager
from src.security import is_safe_session_id
from src.speakers import speaker_memory
from src.storage.db import db
from src.storage.export import export_to_dir, to_markdown
from src.config import APP_DIR, config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/minutes", tags=["minutes"])


def _apply_latest_speaker_labels(minutes_data: dict) -> dict:
    transcript = minutes_data.get("transcript")
    if isinstance(transcript, list):
        minutes_data = dict(minutes_data)
        minutes_data["transcript"] = speaker_memory.apply_latest_labels(transcript)
    return minutes_data


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
    return _apply_latest_speaker_labels(m)


@router.get("/{minutes_id}/transcript")
async def get_transcript(minutes_id: str) -> dict:
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    transcript = m.get("transcript", [])
    if isinstance(transcript, list):
        transcript = speaker_memory.apply_latest_labels(transcript)
    return {"transcript": transcript}


@router.get("/{minutes_id}/markdown")
async def get_markdown(minutes_id: str) -> dict:
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    m = _apply_latest_speaker_labels(m)
    project = project_manager.get(m["project_id"])
    project_name = project.name if project else None
    return {"content": to_markdown(m, project_name)}


@router.post("/{minutes_id}/export")
async def export_minutes(minutes_id: str) -> dict:
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    m = _apply_latest_speaker_labels(m)
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
    return _apply_latest_speaker_labels(updated)


class SummaryUpdateRequest(BaseModel):
    summary: str = Field(max_length=200_000)


@router.patch("/{minutes_id}/summary")
async def update_summary(minutes_id: str, body: SummaryUpdateRequest) -> dict:
    """ユーザーによる要約手動編集。LLM モデル列は更新しない(provenance を保持)。"""
    ok = db.update_summary(minutes_id, body.summary)
    if not ok:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    updated = db.get_minutes(minutes_id)
    if updated is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")
    return _apply_latest_speaker_labels(updated)


@router.put("/{minutes_id}/project")
async def reassign_project(minutes_id: str, body: dict) -> dict:
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
    """既存セッションの音声を使って文字起こしを再実行する。
    バックグラウンドで走り、進捗は _pipelines 経由で WS broadcast される。
    """
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")

    session_id = m.get("session_id", "")
    if not session_id:
        raise not_found("SESSION_NOT_FOUND", "セッションIDが見つかりません")

    # 音声ファイルを探す
    session_dir = APP_DIR / "sessions" / session_id
    wav_path: Path | None = None
    for name in ("combined.flac", "combined.wav", "system.wav", "mic.wav"):
        cand = session_dir / name
        if cand.exists() and cand.stat().st_size > 44:
            wav_path = cand
            break
    if wav_path is None:
        raise not_found("AUDIO_NOT_FOUND",
                        "音声ファイルが見つかりません(セッションが既に削除されている可能性)")

    # 同じ session_id で何かが走っていれば拒否(再実行は1セッションにつき1度ずつ)
    from src.api import recording as rec_mod
    existing = rec_mod._pipelines.get(session_id)
    if existing and existing.get("state") in ("recording", "stopping", "transcribing"):
        raise conflict("ALREADY_RUNNING", "このセッションは現在処理中です")

    cancel_event = threading.Event()
    _retranscribe_cancel_events[session_id] = cancel_event

    task = asyncio.create_task(
        _run_retranscribe(
            minutes_id,
            session_id,
            str(wav_path),
            m.get("project_id", ""),
            cancel_event,
        )
    )
    _retranscribe_tasks[session_id] = task

    def _cleanup_task(done: asyncio.Task) -> None:
        current = _retranscribe_tasks.get(session_id)
        if current is done:
            _retranscribe_tasks.pop(session_id, None)

    task.add_done_callback(_cleanup_task)
    return {"status": "started", "session_id": session_id}


# 再文字起こしの逐次実行ロック(同時1件まで)
_retranscribe_sem = asyncio.Semaphore(1)
_retranscribe_tasks: dict[str, asyncio.Task] = {}
_retranscribe_cancel_events: dict[str, threading.Event] = {}


class RetranscribeCancelledError(RuntimeError):
    """再文字起こしの協調キャンセル通知。"""


@router.post("/{minutes_id}/retranscribe/cancel")
async def cancel_retranscribe_minutes(minutes_id: str) -> dict:
    """進行中の再文字起こし停止を要求する。"""
    m = db.get_minutes(minutes_id)
    if m is None:
        raise not_found("MINUTES_NOT_FOUND", f"議事録 '{minutes_id}' が見つかりません")

    session_id = str(m.get("session_id") or "").strip()
    if not session_id:
        raise not_found("SESSION_NOT_FOUND", "セッションIDが見つかりません")

    from src.api import recording as rec_mod
    existing = rec_mod._pipelines.get(session_id)
    cancel_event = _retranscribe_cancel_events.get(session_id)
    running_task = _retranscribe_tasks.get(session_id)

    if not existing or existing.get("state") not in ("transcribing", "stopping"):
        if running_task and not running_task.done():
            if cancel_event is None:
                cancel_event = threading.Event()
                _retranscribe_cancel_events[session_id] = cancel_event
            cancel_event.set()
            return {"status": "cancelling", "session_id": session_id}
        return {"status": "not_running", "session_id": session_id}

    if cancel_event is None:
        cancel_event = threading.Event()
        _retranscribe_cancel_events[session_id] = cancel_event
    cancel_event.set()

    from src.pipeline.state import Stage, STAGE_LABELS
    existing.update({
        "state": "stopping",
        "stage": Stage.CANCELLED.value,
        "stage_label": STAGE_LABELS[Stage.CANCELLED],
        "message": "停止要求を受け付けました...",
    })
    await rec_mod._broadcast_pipeline(session_id)
    return {"status": "cancelling", "session_id": session_id}


def _load_wav_16k_mono_f32(path: str):
    """音声ファイルを 16kHz mono float32 numpy 配列で読み込む。

    WAV は wave で直接読み、それ以外 (FLAC など) は ffmpeg で f32le mono にデコードする。
    """
    import numpy as np
    from pathlib import Path as _Path

    ext = _Path(path).suffix.lower()
    if ext == ".wav":
        import wave
        from math import gcd
        with wave.open(path, "rb") as wf:
            sr = wf.getframerate()
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            nframes = wf.getnframes()
            frames = wf.readframes(nframes)

        if sampwidth == 2:
            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        elif sampwidth == 4:
            audio = np.frombuffer(frames, dtype=np.float32)
        elif sampwidth == 1:
            audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128) / 128.0
        else:
            raise ValueError(f"Unsupported sample width: {sampwidth}")

        if nchannels > 1:
            audio = audio.reshape(-1, nchannels).mean(axis=1).astype(np.float32)

        if sr != 16000:
            from scipy.signal import resample_poly
            g = gcd(16000, sr)
            audio = resample_poly(audio, 16000 // g, sr // g).astype(np.float32)
        return audio

    # FLAC など: ffmpeg で 16kHz mono f32le に直接デコード
    import subprocess
    from src.audio.recorder import FFMPEG
    proc = subprocess.run(
        [FFMPEG, "-loglevel", "error", "-i", path,
         "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {proc.stderr[-500:]!r}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


async def _run_retranscribe(minutes_id: str, session_id: str,
                            wav_path: str, project_id: str,
                            cancel_event: threading.Event) -> None:
    """再文字起こし処理。
    - 同時実行は 1 件のみ(_retranscribe_sem)。複数キューイング時は FIFO 待ち
    - 録音中は待機して、新録音とそのストリーミング文字起こしを最優先
    - 安定性向上のため、streaming と同じ VADChunker でチャンク化してから個別に transcribe
    """
    from src.api import recording as rec_mod
    from src.api.ws import ws_manager

    from src.pipeline.state import Stage, STAGE_LABELS, RETRANSCRIBE_PIPELINE

    # pipeline 登録(キュー待ち)
    rec_mod._pipelines[session_id] = {
        "session_id": session_id,
        "project_id": project_id,
        "state": "transcribing",
        "stage": Stage.QUEUED.value,
        "stage_label": STAGE_LABELS[Stage.QUEUED],
        "stage_step": 1,
        "stage_total": len(RETRANSCRIBE_PIPELINE),
        "message": "キューで待機中...",
        "result": None,
        "transcript": None,
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    rec_mod._evict_completed_pipelines()
    await rec_mod._broadcast_pipeline(session_id)

    def _set_retry_stage(stage: Stage, message: str | None = None) -> None:
        """再文字起こし pipeline 内の stage 更新ヘルパー (worker thread から
        呼ぶ場合は run_coroutine_threadsafe で _broadcast_pipeline を投げる)。"""
        idx = RETRANSCRIBE_PIPELINE.index(stage) if stage in RETRANSCRIBE_PIPELINE else 0
        rec_mod._pipelines[session_id].update({
            "stage": stage.value,
            "stage_label": STAGE_LABELS[stage],
            "stage_step": idx + 1,
            "stage_total": len(RETRANSCRIBE_PIPELINE),
            "message": message or STAGE_LABELS[stage],
        })

    def _assert_not_cancelled() -> None:
        if cancel_event.is_set():
            raise RetranscribeCancelledError("ユーザーにより停止されました")

    # 同時1件 FIFO
    async with _retranscribe_sem:
        _assert_not_cancelled()
        # 録音中なら録音終了まで待機
        while rec_mod._active_session_id is not None:
            _assert_not_cancelled()
            _set_retry_stage(Stage.QUEUED, "録音中のため待機中...")
            await rec_mod._broadcast_pipeline(session_id)
            await asyncio.sleep(2)

        _assert_not_cancelled()
        _set_retry_stage(Stage.WHISPER_LOAD, "Whisperモデル準備中... (初回はダウンロードに数分)")
        rec_mod._pipelines[session_id]["progress"] = 0.0
        await rec_mod._broadcast_pipeline(session_id)

        loop = asyncio.get_event_loop()

        def do_transcribe() -> list[dict]:
            import os
            try:
                os.nice(10)
            except (OSError, AttributeError):
                pass

            def _assert_not_cancelled_sync() -> None:
                if cancel_event.is_set():
                    raise RetranscribeCancelledError("ユーザーにより停止されました")

            import mlx_whisper
            from src.transcribe.hallucination_filter import HallucinationFilter
            from src.transcribe.streaming import (
                _resolve_repo, get_or_load_model, VADChunker, SileroVADChunker,
                SAMPLE_RATE, build_initial_prompt, PROMPT_RECENT_CHARS,
            )

            model_name = config.get("whisper", "model", default="medium")
            repo = _resolve_repo(model_name)
            # モデル読込中の進捗表示 (worker thread → loop に投げる)
            def _emit_stage(stage: Stage, message: str | None = None) -> None:
                try:
                    _set_retry_stage(stage, message)
                    asyncio.run_coroutine_threadsafe(
                        rec_mod._broadcast_pipeline(session_id), loop,
                    )
                except Exception:
                    pass
            _assert_not_cancelled_sync()
            _emit_stage(Stage.WHISPER_LOAD, f"Whisper モデル読込中 ({model_name})")
            get_or_load_model(model_name)
            _assert_not_cancelled_sync()
            _emit_stage(Stage.AUDIO_ANALYZE, "音声を発話単位に分割中...")

            audio = _load_wav_16k_mono_f32(wav_path)
            _assert_not_cancelled_sync()
            language = config.get("whisper", "language", default="ja")

            # プロジェクトの用語集 + corrections の正式表記を initial_prompt として使う
            project = project_manager.get(project_id)
            glossary = list(project.glossary) if project else []
            corrections_pairs: list[tuple[str, str]] = []
            if project and project.corrections:
                for _c in project.corrections:
                    if _c.correct and _c.correct not in glossary:
                        glossary.append(_c.correct)
                    if _c.wrong and _c.correct and _c.wrong != _c.correct:
                        corrections_pairs.append((_c.wrong, _c.correct))
            # 長いキーから順に置換 (短いキーが部分一致して長いキーを破壊しないように)
            corrections_pairs.sort(key=lambda x: len(x[0]), reverse=True)

            hall_filter = HallucinationFilter.from_config(
                config.get("whisper", "hallucination_filter", default={}) or {}
            )

            # streaming と同じ VAD で発話単位に分割 → 各チャンクを個別 transcribe
            streaming_cfg = config.get("whisper", "streaming", default={}) or {}
            vad_provider = str(streaming_cfg.get("vad_provider", "silero")).lower()
            silence_ms = int(streaming_cfg.get("silence_duration_ms", 500))
            min_ms = int(streaming_cfg.get("min_chunk_ms", 1000))
            max_ms = int(streaming_cfg.get("max_chunk_ms", 12000))
            chunker: object
            if vad_provider == "silero":
                try:
                    chunker = SileroVADChunker(
                        threshold=float(streaming_cfg.get("silero_threshold", 0.5)),
                        silence_duration_ms=silence_ms,
                        min_chunk_ms=min_ms,
                        max_chunk_ms=max_ms,
                    )
                except Exception as e:
                    logger.warning(
                        "[retry] Silero VAD init failed (%s), falling back to energy", e,
                    )
                    chunker = VADChunker(
                        rms_threshold=float(streaming_cfg.get("rms_threshold", 0.005)),
                        silence_duration_ms=silence_ms,
                        min_chunk_ms=min_ms,
                        max_chunk_ms=max_ms,
                    )
            else:
                chunker = VADChunker(
                    rms_threshold=float(streaming_cfg.get("rms_threshold", 0.005)),
                    silence_duration_ms=silence_ms,
                    min_chunk_ms=min_ms,
                    max_chunk_ms=max_ms,
                )
            BLOCK = SAMPLE_RATE // 10  # 100ms

            jobs: list[tuple] = []  # (chunk_audio, start_offset_sec)
            elapsed = 0.0
            for i in range(0, max(0, len(audio) - BLOCK + 1), BLOCK):
                _assert_not_cancelled_sync()
                block = audio[i:i + BLOCK]
                if len(block) == 0:
                    break
                elapsed += len(block) / SAMPLE_RATE
                ch = chunker.feed(block)
                if ch is not None:
                    dur = len(ch) / SAMPLE_RATE
                    start = max(0.0, elapsed - dur)
                    jobs.append((ch, start))
            final = chunker.flush()
            if final is not None:
                dur = len(final) / SAMPLE_RATE
                start = max(0.0, elapsed - dur)
                jobs.append((final, start))

            segments: list[dict] = []
            filtered_count = 0
            recent_tail = ""
            total_jobs = max(1, len(jobs))
            last_progress_emit = 0.0
            # 文字起こしフェーズへ移行
            _emit_stage(Stage.TRANSCRIBE)

            def _emit_progress(done_idx: int, message: str | None = None) -> None:
                """worker スレッドから loop に WS broadcast をスケジュール (throttled)。"""
                nonlocal last_progress_emit
                progress = min(1.0, done_idx / total_jobs)
                rec_mod._pipelines[session_id]["progress"] = round(progress, 3)
                if message is not None:
                    rec_mod._pipelines[session_id]["message"] = message
                else:
                    pct = int(progress * 100)
                    rec_mod._pipelines[session_id]["message"] = (
                        f"再文字起こし中... {done_idx}/{total_jobs} ({pct}%)"
                    )
                try:
                    import time as _t
                    now_t = _t.monotonic()
                    if now_t - last_progress_emit < 0.4 and done_idx < total_jobs:
                        return
                    last_progress_emit = now_t
                    asyncio.run_coroutine_threadsafe(
                        rec_mod._broadcast_pipeline(session_id), loop,
                    )
                except Exception as e:
                    logger.debug("progress emit failed: %s", e)

            _emit_progress(0, message=f"再文字起こし中... 0/{total_jobs} (0%)")
            for idx, (chunk_audio, start_offset) in enumerate(jobs):
                _assert_not_cancelled_sync()
                initial_prompt = build_initial_prompt(glossary, recent_tail)
                kwargs: dict = {
                    "path_or_hf_repo": repo,
                    "language": language,
                    # word-level タイムスタンプを有効化 (話者ターン境界での再分割に使う)
                    "word_timestamps": True,
                    "verbose": False,
                    # チャンク内の複数 segment をデコードする際、前の segment 出力を
                    # 文脈として活用 (表記揺れを抑える)。
                    "condition_on_previous_text": True,
                    # 単一温度 (フォールバック無効化で高速化)。streaming と同じ挙動。
                    # ハルシネーションは別途 HallucinationFilter で除去。
                    "temperature": 0.0,
                    "compression_ratio_threshold": 2.4,
                    "logprob_threshold": -1.0,
                    "no_speech_threshold": 0.6,
                    # 無音区間でのハルシネーション抑制
                    "hallucination_silence_threshold": 2.0,
                }
                if initial_prompt:
                    kwargs["initial_prompt"] = initial_prompt
                try:
                    result = mlx_whisper.transcribe(
                        chunk_audio.astype("float32"),
                        **kwargs,
                    )
                except Exception as e:
                    logger.warning("retry chunk %d failed: %s", idx, e)
                    continue
                chunk_texts: list[str] = []
                for seg in result.get("segments", []):
                    text = (seg.get("text") or "").strip()
                    if not text:
                        continue
                    if hall_filter.is_hallucination(text):
                        filtered_count += 1
                        logger.info("[retry] Filtered hallucination: %s", text[:80])
                        continue
                    # corrections (wrong→correct) を適用
                    if corrections_pairs:
                        for wrong, correct in corrections_pairs:
                            if wrong in text:
                                text = text.replace(wrong, correct)
                    # words: グローバルタイムスタンプに変換して保持
                    words_raw = seg.get("words") or []
                    words: list[dict] = []
                    for w in words_raw:
                        wt = (w.get("word") or "").strip()
                        if not wt:
                            continue
                        try:
                            ws = float(w.get("start", 0))
                            we = float(w.get("end", 0))
                        except Exception:
                            continue
                        words.append({
                            "start": round(start_offset + ws, 3),
                            "end": round(start_offset + we, 3),
                            "word": wt,
                        })
                    segments.append({
                        "start": round(start_offset + float(seg.get("start", 0)), 2),
                        "end": round(start_offset + float(seg.get("end", 0)), 2),
                        "text": text,
                        "words": words,
                    })
                    chunk_texts.append(text)
                if chunk_texts:
                    merged = (recent_tail + " " + " ".join(chunk_texts)).strip()
                    recent_tail = merged[-PROMPT_RECENT_CHARS:]
                _emit_progress(idx + 1)
            _assert_not_cancelled_sync()
            if filtered_count:
                logger.info(
                    "[retry] Hallucination filter dropped %d segments", filtered_count,
                )
            return segments

        import time as _time
        async def _publish_cancelled(message: str = "再文字起こしを停止しました") -> None:
            _set_retry_stage(Stage.CANCELLED, message)
            rec_mod._pipelines[session_id].update({
                "state": "done",
                "message": message,
                "error": None,
                "progress": 0.0,
            })
            rec_mod._evict_completed_pipelines()
            await ws_manager.broadcast({
                "type": "pipeline_done",
                "data": {
                    "session_id": session_id,
                    "minutes_id": minutes_id,
                    "retranscribe": True,
                    "cancelled": True,
                },
            })
            await rec_mod._broadcast_pipeline(session_id)

        try:
            _assert_not_cancelled()
            t0 = _time.perf_counter()
            segments = await loop.run_in_executor(None, do_transcribe)
            t1 = _time.perf_counter()
            _assert_not_cancelled()
            logger.info(
                "[retry] transcribe done in %.1fs (%d segments)",
                t1 - t0, len(segments),
            )

            _set_retry_stage(Stage.DIARIZE_EXTRACT)
            rec_mod._pipelines[session_id]["progress"] = 0.0
            await rec_mod._broadcast_pipeline(session_id)

            _last_diarize_emit = 0.0

            def _on_diarize_progress(stage_key: str, prog: float, message: str | None) -> None:
                nonlocal _last_diarize_emit
                stage_enum = {
                    "extract": Stage.DIARIZE_EXTRACT,
                    "cluster": Stage.DIARIZE_CLUSTER,
                    "assign": Stage.DIARIZE_CLUSTER,
                }.get(stage_key, Stage.DIARIZE_EXTRACT)
                _set_retry_stage(stage_enum, message)
                rec_mod._pipelines[session_id]["progress"] = round(prog, 3)
                now_t = _time.monotonic()
                if prog < 1.0 and (now_t - _last_diarize_emit) < 0.2:
                    return
                _last_diarize_emit = now_t
                try:
                    asyncio.run_coroutine_threadsafe(
                        rec_mod._broadcast_pipeline(session_id), loop,
                    )
                except Exception:
                    pass

            _assert_not_cancelled()
            t2 = _time.perf_counter()
            segments = await loop.run_in_executor(
                None,
                lambda: speaker_memory.rediarize_segments(
                    segments,
                    wav_path=wav_path,
                    session_id=session_id,
                    on_progress=_on_diarize_progress,
                ),
            )
            t3 = _time.perf_counter()
            _assert_not_cancelled()
            logger.info(
                "[retry] diarization done in %.1fs (%d segments after split)",
                t3 - t2, len(segments),
            )

            _set_retry_stage(Stage.SAVING)
            await rec_mod._broadcast_pipeline(session_id)
            try:
                db.update_transcript(minutes_id, segments)
            except Exception as e:
                logger.error("update_transcript failed: %s", e)

            transcript_payload = rec_mod.build_pipeline_transcript_payload(segments)
            _set_retry_stage(Stage.DONE, "再文字起こし完了")
            rec_mod._pipelines[session_id].update({
                "state": "done",
                "progress": 1.0,
                "transcript": transcript_payload,
            })
            rec_mod._evict_completed_pipelines()
            await ws_manager.broadcast({
                "type": "pipeline_done",
                "data": {
                    "session_id": session_id,
                    "minutes_id": minutes_id,
                    "retranscribe": True,
                    "count": len(segments),
                },
            })
            await rec_mod._broadcast_pipeline(session_id)
            logger.info("Retranscribe done: %s (%d segments)", session_id, len(segments))

        except RetranscribeCancelledError:
            await _publish_cancelled()
            logger.info("Retranscribe cancelled: %s", session_id)
        except asyncio.CancelledError:
            await _publish_cancelled()
            logger.info("Retranscribe task cancelled: %s", session_id)
        except Exception as e:
            logger.error("Retranscribe failed: %s", e)
            rec_mod._pipelines[session_id].update({
                "state": "error",
                "message": str(e),
                "error": str(e),
            })
            rec_mod._evict_completed_pipelines()
            await ws_manager.broadcast({
                "type": "pipeline_error",
                "data": {"session_id": session_id, "message": str(e)},
            })
            await rec_mod._broadcast_pipeline(session_id)
        finally:
            _retranscribe_tasks.pop(session_id, None)
            _retranscribe_cancel_events.pop(session_id, None)
