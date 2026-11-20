"""録音と Apple Speech 文字起こしのパイプラインを管理する。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.api.errors import bad_request, conflict, internal_error, not_found
from src.api.ws import ws_manager
from src.audio.converter import convert_audio_to_mp3
from src.audio.mic_capture import list_microphones
from src.audio.mixer import RealtimeMixer
from src.audio.mixed_writer import RealtimeMixedAudioWriter
from src.audio.recorder import FFMPEG, SESSIONS_DIR, recorder
from src.audio.resource_monitor import ResourceMonitor
from src.config import config
from src.security import is_loopback_client_host, is_safe_session_id
from src.transcribe.apple_speech import AppleSpeechProcess, start_live_transcription

logger = logging.getLogger(__name__)

SESSION_META_FILENAME = "session_meta.json"
TRANSCRIPT_JSONL_FILENAME = "transcript.jsonl"
AUDIO_FILE_PRIORITY = ("combined.mp3", "combined.flac", "combined.wav", "system.wav", "mic.wav")
# 新規録音のMP3を優先し、既存FLACは再生時にseekable MP3へ変換する。
PLAYBACK_AUDIO_FILE_PRIORITY = ("combined.mp3", "combined.play.mp3")

router = APIRouter(prefix="/api/recording", tags=["recording"])


class StartRequest(BaseModel):
    project_id: str
    mic_device: int | None = None
    capture_system: bool = True


class MicrophoneRequest(BaseModel):
    session_id: str
    device_uid: str | None = Field(default=None, min_length=1)


# ─── State ──────────────────────────────────────────────────────────────

# 録音のセッションは並行できないが、文字起こしは並行可。
# session_id → pipeline メタデータ
_pipelines: dict[str, dict] = {}
# 現在録音中の session_id(録音終了でクリア)
_active_session_id: str | None = None
_recording_paused = False
_recording_start_lock = asyncio.Lock()
_finalize_tasks: dict[str, asyncio.Task] = {}
# session ごとの streamer / mixer
_speech_processes: dict[str, AppleSpeechProcess] = {}
_mixers: dict[str, RealtimeMixer] = {}
_mix_writers: dict[str, RealtimeMixedAudioWriter] = {}

# audio_level 用(録音中のみ意味あり)
_level_task: asyncio.Task | None = None
_latest_level: float = 0.0       # マイク入力 RMS (0.0-1.0)
_latest_system_level: float = 0.0  # システム音声 RMS (0.0-1.0)
_last_result: dict | None = None

# 起動時セッション復元の進捗 (GUI からの照会用)
_recovery_state: dict = {
    "state": "idle",   # idle | running | done
    "total": 0,
    "current": 0,
    "item": None,      # 現在処理中の item
    "recovered": 0,
    "finished_at": None,
}

_ACTIVE_PIPELINE_STATES = {"recording", "stopping", "transcribing"}
_resource_monitor = ResourceMonitor()


def _stop_forget_reminder_settings() -> tuple[bool, float, float]:
    cfg = config.get("recording", "stop_forget_reminder", default={}) or {}
    enabled = bool(cfg.get("enabled", True))
    try:
        silence_sec = float(cfg.get("silence_sec", 300))
    except Exception:
        silence_sec = 300.0
    try:
        level_threshold = float(cfg.get("level_threshold", 0.02))
    except Exception:
        level_threshold = 0.02
    return enabled, max(10.0, silence_sec), max(0.0, level_threshold)


def _max_retained_pipelines() -> int:
    value = int(
        config.get(
            "recording",
            "memory_guard",
            "max_retained_pipelines",
            default=40,
        )
    )
    return max(5, value)


def _max_transcript_segments() -> int:
    value = int(
        config.get(
            "recording",
            "memory_guard",
            "max_transcript_segments_per_pipeline",
            default=600,
        )
    )
    return max(50, value)


def _compact_transcript_segments(segments: list[dict]) -> tuple[list[dict], int]:
    max_segments = _max_transcript_segments()
    total = len(segments)
    if total <= max_segments:
        return segments, 0
    trimmed_head = total - max_segments
    return segments[-max_segments:], trimmed_head


def _require_safe_session_id(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not is_safe_session_id(sid):
        raise bad_request("INVALID_SESSION_ID", "不正な session_id です")
    return sid


def _is_valid_audio_file(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 44
    except Exception:
        return False


def _pick_audio_file(session_dir: Path, priority: tuple[str, ...]) -> Path | None:
    for name in priority:
        path = session_dir / name
        if _is_valid_audio_file(path):
            return path
    return None


def _has_usable_audio_from_result(result: dict | None) -> bool:
    """recorder.stop() の結果に、文字起こし継続可能な音声ファイルがあるか判定する。"""
    if not isinstance(result, dict):
        return False
    for key in ("wav_path", "combined_wav", "mic_wav", "system_wav"):
        raw = result.get(key)
        if not raw:
            continue
        try:
            if _is_valid_audio_file(Path(str(raw))):
                return True
        except Exception:
            continue
    return False


def _is_system_audio_failure(message: str) -> bool:
    text = message.lower()
    return (
        "system audio" in text
        or "core audio" in text
        or "sidecar" in text
        or "システム音声" in message
        or "内部音声" in message
        or "キャプチャ" in message
    )


def _finalize_realtime_mixed_audio(session_id: str, result: dict | None) -> dict:
    updated = dict(result or {})
    writer = _mix_writers.pop(session_id, None)
    if writer is None:
        return updated

    try:
        rec_cfg = config.get("recording", default={}) or {}
        timeout_sec = max(60, min(900, int(rec_cfg.get("finalize_timeout_sec", 300))))
    except Exception:
        timeout_sec = 300

    try:
        mixed_path = writer.finalize(timeout_sec=timeout_sec)
    except Exception as e:
        logger.error("realtime mixed audio finalize failed [%s]: %s", session_id, e)
        try:
            writer.close()
        except Exception:
            pass
        return updated

    if mixed_path is None or not _is_valid_audio_file(mixed_path):
        return updated

    duration = round(float(writer.duration_sec), 1)
    updated["wav_path"] = str(mixed_path)
    updated["combined_wav"] = str(mixed_path)
    updated["duration_sec"] = duration
    _remove_intermediate_track_wavs(updated, keep=mixed_path)
    return updated


def _remove_intermediate_track_wavs(result: dict, *, keep: Path) -> None:
    keep_resolved = keep.resolve()
    for key in ("mic_wav", "system_wav"):
        raw = result.get(key)
        if not raw:
            continue
        try:
            path = Path(str(raw))
            if path.resolve() == keep_resolved:
                continue
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Failed to remove intermediate %s: %s", raw, e)
        result[key] = None


def _pick_playback_audio(session_dir: Path) -> Path | None:
    direct = _pick_audio_file(session_dir, PLAYBACK_AUDIO_FILE_PRIORITY)
    if direct is not None:
        return direct

    flac = session_dir / "combined.flac"
    if _is_valid_audio_file(flac):
        playback = session_dir / "combined.play.mp3"
        try:
            if (
                not _is_valid_audio_file(playback)
                or playback.stat().st_mtime_ns < flac.stat().st_mtime_ns
            ):
                convert_audio_to_mp3(flac, playback, ffmpeg_bin=FFMPEG)
            if _is_valid_audio_file(playback):
                return playback
        except Exception as e:
            logger.warning("Failed to prepare seekable playback audio %s: %s", flac, e)
        return flac

    return _pick_audio_file(
        session_dir,
        ("combined.wav", "system.wav", "mic.wav", "combined.play.wav"),
    )


def build_pipeline_transcript_payload(segments: list[dict]) -> dict:
    compact_segments, trimmed_head = _compact_transcript_segments(segments)
    payload = {
        "segments": compact_segments,
        "count": len(segments),
        "chars": sum(len(s.get("text", "")) for s in segments),
        "time_sec": 0.0,
    }
    if trimmed_head > 0:
        payload["truncated_head_segments"] = trimmed_head
    return payload


def _estimate_pipeline_payload_bytes() -> int:
    total = 0
    for pipeline in _pipelines.values():
        try:
            total += len(json.dumps(pipeline, ensure_ascii=False))
        except Exception:
            continue
    return total


def _evict_completed_pipelines() -> int:
    limit = _max_retained_pipelines()
    if len(_pipelines) <= limit:
        return 0

    removable = [
        (sid, p) for sid, p in _pipelines.items()
        if sid != _active_session_id and p.get("state") not in _ACTIVE_PIPELINE_STATES
    ]
    removable.sort(key=lambda item: item[1].get("started_at", ""))
    overflow = len(_pipelines) - limit
    removed = 0
    for sid, _ in removable[:overflow]:
        _pipelines.pop(sid, None)
        removed += 1
    if removed:
        logger.info("Evicted %d completed pipelines (keep=%d)", removed, limit)
    return removed


def get_runtime_debug_snapshot() -> dict:
    sid = _active_session_id
    states: dict[str, int] = {}
    for p in _pipelines.values():
        state = str(p.get("state") or "unknown")
        states[state] = states.get(state, 0) + 1

    active_pipeline = dict(_pipelines[sid]) if sid and sid in _pipelines else None
    active_mixer = _mixers.get(sid).get_debug_snapshot() if sid and sid in _mixers else None

    sessions = []
    for session_id, p in sorted(_pipelines.items(), key=lambda x: x[1].get("started_at", ""), reverse=True):
        mx = _mixers.get(session_id)
        sessions.append({
            "session_id": session_id,
            "state": p.get("state"),
            "project_id": p.get("project_id"),
            "started_at": p.get("started_at"),
            "mixer": mx.get_debug_snapshot() if mx else None,
        })

    return {
        "recording": recorder.get_status(),
        "active_session_id": sid,
        "latest_audio_level": round(_latest_level, 3),
        "pipelines_total": len(_pipelines),
        "pipelines_estimated_mb": round(_estimate_pipeline_payload_bytes() / (1024 * 1024), 2),
        "memory_guard": {
            "max_retained_pipelines": _max_retained_pipelines(),
            "max_transcript_segments_per_pipeline": _max_transcript_segments(),
        },
        "resource": {
            "system": _resource_monitor.snapshot(),
        },
        "pipelines_by_state": states,
        "active_pipeline": active_pipeline,
        "active_mixer": active_mixer,
        "sessions": sessions,
    }


def _session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def _write_session_meta(session_id: str, **fields) -> bool:
    """セッションの永続メタ情報を atomic に上書き保存。

    クラッシュ後、起動時 recover_pending_sessions がこの meta を見て復元判定する。
    """
    try:
        d = _session_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / SESSION_META_FILENAME
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing.update(fields)
        existing["session_id"] = session_id
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning("session_meta write failed [%s]: %s", session_id, e)
        return False


def _delete_session_meta(session_id: str) -> None:
    try:
        (_session_dir(session_id) / SESSION_META_FILENAME).unlink(missing_ok=True)
    except Exception:
        pass


def _enqueue_auto_summary(minutes_id: str) -> bool:
    """要約を永続キューへ積む。同じ議事録が既に待機中なら再投入しない。"""
    try:
        from src.summarize.runner import get_runner

        runner = get_runner()
        if runner.ensure_queued(minutes_id):
            return True
        logger.warning(
            "[summary] auto-summary is active but its restart queue could not be saved: %s",
            minutes_id,
        )
        return False
    except Exception as e:
        logger.warning("[summary] failed to enqueue auto-summary for %s: %s", minutes_id, e)
        return False


def _remove_empty_session_dir(session_id: str) -> None:
    """初期化に失敗したセッションの空ディレクトリだけを片付ける。"""
    try:
        _session_dir(session_id).rmdir()
    except OSError:
        # 音声など何か残っていれば、救済できるようそのまま保持する。
        pass


def _append_transcript_segment(session_id: str, segment: dict) -> None:
    """確定 segment を transcript.jsonl に追記。finalize 中クラッシュ時のフォールバック用。"""
    try:
        d = _session_dir(session_id)
        if not d.exists():
            return
        path = d / TRANSCRIPT_JSONL_FILENAME
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(segment, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("transcript jsonl append failed [%s]: %s", session_id, e)


def _load_persisted_segments(session_id: str) -> list[dict]:
    path = _session_dir(session_id) / TRANSCRIPT_JSONL_FILENAME
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict) and str(item.get("text") or "").strip():
                    segment = {
                        "start": float(item.get("start") or 0),
                        "end": float(item.get("end") or 0),
                        "text": str(item["text"]).strip(),
                    }
                    if item.get("session_id"):
                        segment["session_id"] = str(item["session_id"])
                    out.append(segment)
            except Exception:
                continue
    except Exception as e:
        logger.warning("transcript jsonl read failed [%s]: %s", session_id, e)
    return out


def _new_pipeline(session_id: str, project_id: str, capture_system: bool = False) -> dict:
    return {
        "session_id": session_id,
        "project_id": project_id,
        "capture_system": capture_system,
        "state": "recording",
        "message": "",
        "result": None,
        "transcript": None,
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def _set_state(session_id: str, **updates) -> dict | None:
    p = _pipelines.get(session_id)
    if p is None:
        return None
    p.update(updates)
    if p.get("state") in {"done", "error"}:
        _evict_completed_pipelines()
    return p


def _on_level(level: float) -> None:
    global _latest_level
    _latest_level = float(level)


def _make_system_level_tap(forward):
    """system_pcm_callback の薄いラッパー。RMS を _latest_system_level に反映してから mixer に流す。"""
    import numpy as _np

    def _tap(samples, sample_rate: int = 48000) -> None:
        global _latest_system_level
        try:
            if samples is not None and len(samples):
                arr = _np.asarray(samples)
                if arr.dtype.kind == "i":
                    rms = float(_np.sqrt(_np.mean(arr.astype(_np.float32) ** 2))) / 32768.0
                else:
                    rms = float(_np.sqrt(_np.mean(arr.astype(_np.float32) ** 2)))
                _latest_system_level = min(1.0, rms * 5)
        except Exception:
            pass
        try:
            forward(samples, sample_rate)
        except Exception as e:
            logger.error("System pcm forward error: %s", e)

    return _tap


def _combined_level() -> float:
    """mic + system のうち大きい方を返す。無音判定はこの値で行う。"""
    if _recording_paused:
        return 0.0
    return max(_latest_level, _latest_system_level)


async def _stream_levels() -> None:
    """録音中の WS 配信 + ワーカースレッド監視(watchdog) + 無音リマインド。"""
    import time as _time
    import psutil  # 遅延 import

    last_level = 0.0
    last_status = 0.0
    last_watchdog = 0.0
    reminder_enabled, reminder_silence_sec, reminder_level_threshold = _stop_forget_reminder_settings()
    silent_since: float | None = None
    reminded_in_current_silence = False
    # マイク無音早期検知: 録音開始後一定時間ずっと level=0 ならハードウェア/権限の異常
    mic_seen_audio = False
    mic_silent_warned = False
    mic_failure_handled = False
    MIC_SILENT_WARN_SEC = 15.0   # 15秒以上 level がほぼ0なら警告
    MIC_AUDIO_THRESHOLD = 0.005  # この値を超えたら有効音声と判定
    while recorder.is_recording:
        sid = _active_session_id
        try:
            cpu_pct = psutil.cpu_percent(interval=None)
        except Exception:
            cpu_pct = 0
        scale = 1.5 if cpu_pct >= 80 else 1.0

        now = _time.monotonic()

        # Fixed cadence so the meter also shows elapsed time and silence.
        if (now - last_level) >= 0.1 * scale:
            await ws_manager.broadcast({
                "type": "audio_level",
                "data": {
                    "session_id": sid,
                    "mic": 0.0 if _recording_paused else round(_latest_level, 3),
                    "level": 0.0 if _recording_paused else round(_combined_level(), 3),
                },
            })
            last_level = now

            # 一度でも有効音声を検出したらフラグ立て (mic / system のどちらかでも OK)
            if not mic_seen_audio and _combined_level() >= MIC_AUDIO_THRESHOLD:
                mic_seen_audio = True

        # 録音開始から MIC_SILENT_WARN_SEC 経っても全く音を拾っていなければ警告 (1度だけ)
        # mic + system 両方とも無音だった場合のみ警告 (system audio 単独録音も正常扱い)
        if (
            not mic_silent_warned
            and not mic_seen_audio
            and not _recording_paused
            and sid
            and recorder.elapsed_sec >= MIC_SILENT_WARN_SEC
        ):
            mic_silent_warned = True
            active_elapsed = recorder.elapsed_sec
            mic_err = recorder.error if hasattr(recorder, "error") else None
            logger.warning(
                "[mic-silent] no audio detected (mic+system) for %.1fs (session=%s, error=%s)",
                active_elapsed, sid, mic_err,
            )
            await ws_manager.broadcast({
                "type": "mic_silent_warning",
                "data": {
                    "session_id": sid,
                    "since_sec": int(active_elapsed),
                    "error": mic_err,
                },
            })

        # recording_status は高負荷時も1秒間隔を維持する。
        if (now - last_status) >= 1.0:
            await ws_manager.broadcast({
                "type": "recording_status",
                "data": {
                    "state": "recording",
                    "elapsed_sec": round(recorder.elapsed_sec, 1),
                    "session_id": sid,
                    "paused": _recording_paused,
                    "transcription_deferred": bool((_pipelines.get(sid) or {}).get("transcription_deferred")),
                },
            })
            last_status = now

        # stop-forget reminder: 無音が一定時間続いたら通知(停止はしない)。
        # mic + system いずれも閾値未満のときだけ「無音」扱い。
        if _recording_paused:
            silent_since = None
            reminded_in_current_silence = False
        elif reminder_enabled and sid:
            if _combined_level() < reminder_level_threshold:
                if silent_since is None:
                    silent_since = now
                    reminded_in_current_silence = False
                silent_for_sec = now - silent_since
                if (not reminded_in_current_silence) and silent_for_sec >= reminder_silence_sec:
                    logger.info(
                        "Stop-forget reminder fired [%s]: silent_for=%.1fs, threshold=%.1fs",
                        sid, silent_for_sec, reminder_silence_sec,
                    )
                    await ws_manager.broadcast({
                        "type": "recording_idle_reminder",
                        "data": {
                            "session_id": sid,
                            "silent_for_sec": int(silent_for_sec),
                            "threshold_sec": int(reminder_silence_sec),
                        },
                    })
                    reminded_in_current_silence = True
            else:
                silent_since = None
                reminded_in_current_silence = False

        # watchdog: 5秒に1回、録音入力とミキサーを確認。
        if (now - last_watchdog) >= 5.0 and sid:
            try:
                if (
                    not mic_failure_handled
                    and recorder.error
                    and not recorder.mic_stream_alive
                ):
                    mic_failure_handled = True
                    msg = str(recorder.error)
                    logger.error("[watchdog] mic stream dead for %s: %s", sid, msg)
                    await _stop_failed_active_recording(sid, msg)
                    return

                mixer = _mixers.get(sid)
                if mixer is not None and not mixer.consumer_alive:
                    logger.error("[watchdog] mixer consumer dead for %s", sid)
                    mixer.restart_consumer("watchdog dead")
            except Exception as e:
                logger.error("[watchdog] check failed: %s", e)
            last_watchdog = now

        await asyncio.sleep(0.1)


def _make_emit_speech_event(
    session_id: str, *, persist: bool = True, broadcast_errors: bool = True,
):
    async def emit(event: dict) -> None:
        event_type = str(event.get("type") or "")
        data = event.get("data")
        if not isinstance(data, dict):
            return
        payload = {**data, "session_id": session_id}
        try:
            if event_type == "transcript_chunk":
                segment = {
                    "start": float(payload.get("start") or 0),
                    "end": float(payload.get("end") or 0),
                    "text": str(payload.get("text") or "").strip(),
                    "session_id": session_id,
                }
                if segment["text"]:
                    if persist:
                        _append_transcript_segment(session_id, segment)
                    logger.info("Apple Speech segment [%s]: %s", session_id, segment["text"][:40])
                    await ws_manager.broadcast({"type": event_type, "data": segment})
            elif event_type == "transcript_preview" or (
                event_type == "transcription_error" and broadcast_errors
            ):
                await ws_manager.broadcast({"type": event_type, "data": payload})
        except Exception:
            logger.exception("Apple Speech event broadcast failed [%s]", session_id)
    return emit


async def _broadcast_pipeline(session_id: str) -> None:
    p = _pipelines.get(session_id)
    if p is None:
        return
    await ws_manager.broadcast({"type": "pipeline_progress", "data": dict(p)})


async def _mark_pipeline_error(session_id: str, message: str) -> None:
    _set_state(session_id, state="error", message=message, error=message)
    await ws_manager.broadcast({
        "type": "pipeline_error",
        "data": {"session_id": session_id, "message": message},
    })
    await _broadcast_pipeline(session_id)


async def _stop_failed_active_recording(session_id: str, message: str) -> None:
    """録音中の致命的な入力異常を停止状態へ寄せ、次の録音を塞がないようにする。"""
    global _active_session_id, _last_result

    _write_session_meta(session_id, state="stopping", error=message)
    await _mark_pipeline_error(session_id, message)

    result: dict | None = None
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, recorder.stop)
        _last_result = result
        _set_state(session_id, result=result)
        _write_session_meta(
            session_id,
            state="stopping",
            error=message,
            wav_path=result.get("wav_path") if isinstance(result, dict) else None,
        )
        await ws_manager.broadcast({
            "type": "recording_stopped",
            "data": {**result, "session_id": session_id},
        })
    except Exception as e:
        logger.error("failed to stop broken recording [%s]: %s", session_id, e)
        _write_session_meta(session_id, state="stopping", error=f"{message}; stop failed: {e}")
    finally:
        if _active_session_id == session_id:
            _active_session_id = None
        _cleanup_session(session_id)
        await _broadcast_pipeline(session_id)


async def _finalize_session(session_id: str) -> None:
    """録音を閉じ、Apple Speech の最終結果を待って議事録を保存する。"""
    p = _pipelines.get(session_id)
    if p is None:
        logger.error("finalize: session %s not found", session_id)
        return

    loop = asyncio.get_running_loop()
    from src.pipeline.state import Stage, STAGE_LABELS, RECORDING_PIPELINE

    def _set_record_stage(stage: Stage, message: str | None = None) -> None:
        idx = RECORDING_PIPELINE.index(stage) if stage in RECORDING_PIPELINE else 0
        _set_state(
            session_id,
            stage=stage.value,
            stage_label=STAGE_LABELS[stage],
            stage_step=idx + 1,
            stage_total=len(RECORDING_PIPELINE),
            message=message or STAGE_LABELS[stage],
        )

    _set_state(session_id, state="stopping")
    _set_record_stage(Stage.STOPPING, "録音を停止中...")
    _write_session_meta(session_id, state="stopping")
    await _broadcast_pipeline(session_id)

    try:
        result = await loop.run_in_executor(None, recorder.stop)
        global _last_result, _active_session_id
        _last_result = result
        _set_state(session_id, result=result)
        recorder_error = str(result.get("error") or "").strip()
        mixed_writer = _mix_writers.get(session_id)
        mixed_audio_available = mixed_writer is not None and _is_valid_audio_file(mixed_writer.wav_path)
        if recorder_error and not _has_usable_audio_from_result(result) and not mixed_audio_available:
            _set_state(session_id, state="error", message=recorder_error, error=recorder_error)
            _write_session_meta(session_id, state="stopping", error=recorder_error)
            _active_session_id = None
            _cleanup_session(session_id)
            await ws_manager.broadcast({"type": "pipeline_error", "data": {"session_id": session_id, "message": recorder_error}})
            await _broadcast_pipeline(session_id)
            return
        _active_session_id = None
    except Exception as e:
        logger.exception("recorder.stop failed [%s]", session_id)
        _set_state(session_id, state="error", message=str(e), error=str(e))
        _active_session_id = None
        _cleanup_session(session_id)
        await ws_manager.broadcast({"type": "pipeline_error", "data": {"session_id": session_id, "message": str(e)}})
        await _broadcast_pipeline(session_id)
        return

    _set_state(session_id, state="transcribing")
    _set_record_stage(Stage.APPLE_SPEECH, "Apple Speech の認識を確定中...")
    _write_session_meta(session_id, state="transcribing")
    await _broadcast_pipeline(session_id)

    try:
        mixer = _mixers.get(session_id)
        if mixer is not None:
            await loop.run_in_executor(None, mixer.stop)

        writer = _mix_writers.get(session_id)
        if writer is not None:
            # close WAV first so the helper can drain the complete recording before conversion removes it.
            writer.close()

        speech = _speech_processes.pop(session_id, None)
        speech_failed = False
        if speech is not None:
            try:
                await speech.finish()
            except Exception as e:
                speech_failed = True
                logger.error("Apple Speech finalization failed [%s]: %s", session_id, e)

        segments = _load_persisted_segments(session_id)
        if (speech is None or speech_failed) and not segments and writer is not None:
            try:
                if _is_valid_audio_file(writer.wav_path):
                    await _transcribe_saved_recording(session_id, writer.wav_path)
                    segments = _load_persisted_segments(session_id)
            except Exception as e:
                logger.exception("Apple Speech file fallback failed [%s]", session_id)
                await ws_manager.broadcast({
                    "type": "transcription_error",
                    "data": {"session_id": session_id, "message": str(e)},
                })
        elif speech_failed and segments:
            logger.warning(
                "Apple Speech stopped after %d confirmed segments; keeping the saved transcript [%s]",
                len(segments), session_id,
            )

        result = await loop.run_in_executor(None, _finalize_realtime_mixed_audio, session_id, result)
        _last_result = result
        _set_state(session_id, result=result)
        await ws_manager.broadcast({"type": "recording_stopped", "data": {**result, "session_id": session_id}})

        segments = _load_persisted_segments(session_id)
        transcript_payload = build_pipeline_transcript_payload(segments)
        _set_record_stage(Stage.SAVING, "議事録を保存中...")
        _write_session_meta(
            session_id,
            state="saving",
            wav_path=result.get("wav_path") if isinstance(result, dict) else None,
        )
        await _broadcast_pipeline(session_id)

        from src.storage.db import db
        project_id = p["project_id"]
        duration = (p.get("result") or {}).get("duration_sec", 0)
        now = datetime.now(timezone.utc).isoformat()
        date_str = datetime.now().strftime("%Y-%m-%d")
        title = f"{datetime.now().strftime('%H:%M')} の会議"
        minutes_id = uuid.uuid4().hex[:12]
        db.insert_minutes({
            "id": minutes_id,
            "session_id": session_id,
            "project_id": project_id,
            "title": title,
            "date": date_str,
            "started_at": now,
            "duration_sec": int(duration),
            "transcript": segments,
            "summary": "",
            "llm_model": "",
            "created_at": now,
            "updated_at": now,
        })
        if db.get_minutes(minutes_id) is None:
            raise RuntimeError("inserted minutes not found")
        logger.info("Minutes saved: %s (%d Apple Speech segments)", session_id, len(segments))

        _set_state(session_id, state="done", transcript=transcript_payload)
        await ws_manager.broadcast({
            "type": "pipeline_done",
            "data": {"session_id": session_id, "minutes_id": minutes_id, **transcript_payload},
        })
        await _broadcast_pipeline(session_id)

        auto_gen = bool(config.get("minutes_ai", "auto_generate", default=True))
        summary_queued = _enqueue_auto_summary(minutes_id) if auto_gen else False
        if summary_queued or not auto_gen:
            _delete_session_meta(session_id)
    except Exception as e:
        logger.exception("Apple Speech finalize failed [%s]", session_id)
        await _mark_pipeline_error(session_id, str(e))
        _cleanup_session(session_id)


def _cleanup_session(session_id: str) -> None:
    """途中失敗時にリソース開放。"""
    mixer = _mixers.pop(session_id, None)
    if mixer is not None:
        try:
            mixer.stop()
        except Exception:
            pass
    writer = _mix_writers.pop(session_id, None)
    if writer is not None:
        try:
            writer.close()
        except Exception:
            pass
    speech = _speech_processes.pop(session_id, None)
    if speech is not None:
        try:
            asyncio.get_running_loop().create_task(speech.finish())
        except RuntimeError:
            speech.process.terminate()


# ─── Endpoints ──────────────────────────────────────────────────────────

async def _start_apple_speech(session_id: str, wav_path: Path) -> AppleSpeechProcess:
    language = str(config.get("speech", "language", default="ja") or "ja")
    return await start_live_transcription(
        wav_path,
        session_id,
        language,
        _make_emit_speech_event(session_id, broadcast_errors=False),
    )


async def _transcribe_saved_recording(session_id: str, wav_path: Path) -> None:
    """ライブ認識が起動できなかった場合、保存済み WAV を認識する。"""
    from src.transcribe.apple_speech import start_file_transcription

    language = str(config.get("speech", "language", default="ja") or "ja")
    process = await start_file_transcription(
        wav_path,
        language,
        _make_emit_speech_event(session_id, broadcast_errors=False),
        session_id=session_id,
    )
    await process.wait_file()


@router.post("/start")
async def start_recording(req: StartRequest) -> dict:
    async with _recording_start_lock:
        return await _start_recording(req)


async def _start_recording(req: StartRequest) -> dict:
    global _active_session_id, _level_task, _latest_level, _latest_system_level, _recording_paused
    if recorder.is_recording or _active_session_id is not None:
        raise conflict("ALREADY_RECORDING", "現在録音中です。停止してから新規録音してください")
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 衝突回避: 同秒に再開した場合は連番付与
    suffix = 0
    sid = session_id
    while sid in _pipelines or _session_dir(sid).exists():
        suffix += 1
        sid = f"{session_id}_{suffix}"
    session_id = sid

    pipeline = _new_pipeline(session_id, req.project_id, bool(req.capture_system))
    _pipelines[session_id] = pipeline
    _evict_completed_pipelines()

    _write_session_meta(
        session_id,
        project_id=req.project_id,
        started_at=pipeline["started_at"],
        capture_system=bool(req.capture_system),
        mic_device=req.mic_device,
        state="recording",
    )

    loop = asyncio.get_running_loop()
    rec_cfg = config.get("recording", default={}) or {}
    mix_writer = RealtimeMixedAudioWriter(_session_dir(session_id))
    try:
        mix_writer.start()
    except Exception as e:
        try:
            mix_writer.close()
        except Exception:
            pass
        _cleanup_session(session_id)
        _pipelines.pop(session_id, None)
        _delete_session_meta(session_id)
        _remove_empty_session_dir(session_id)
        raise conflict("MIXED_AUDIO_WRITER_FAILED", f"録音音声の保存準備に失敗: {e}")

    try:
        _speech_processes[session_id] = await _start_apple_speech(session_id, mix_writer.wav_path)
        pipeline["transcription_deferred"] = False
    except Exception as e:
        logger.error("Failed to start Apple Speech helper [%s]: %s", session_id, e)
        pipeline["transcription_deferred"] = True

    def _on_mixed_chunk(samples) -> None:
        if _recording_paused:
            return
        try:
            mix_writer.feed(samples)
        except Exception as e:
            logger.error("Realtime mixed audio write failed [%s]: %s", session_id, e)

    mixer = RealtimeMixer(
        on_chunk=_on_mixed_chunk,
        audio_leveling=rec_cfg.get("audio_leveling"),
    )
    _mixers[session_id] = mixer
    _mix_writers[session_id] = mix_writer
    try:
        mixer.start(has_system=req.capture_system)
    except Exception as e:
        _cleanup_session(session_id)
        _pipelines.pop(session_id, None)
        _delete_session_meta(session_id)
        _remove_empty_session_dir(session_id)
        logger.exception("Failed to initialize realtime audio mixer [%s]", session_id)
        raise internal_error(
            "AUDIO_MIXER_START_FAILED",
            f"音声入力の準備に失敗したため録音を開始できません: {e}",
        )

    try:
        result = recorder.start(
            mic_device=req.mic_device,
            capture_system=req.capture_system,
            level_callback=_on_level,
            pcm_callback=mixer.feed_mic,
            system_pcm_callback=_make_system_level_tap(mixer.feed_system),
            session_id=session_id,
        )
    except Exception as e:
        if req.capture_system and "内部音声" in str(e):
            code = "SYSTEM_AUDIO_START_FAILED"
            message = f"内部音声の録音開始失敗: {e}"
        else:
            code = "RECORDER_START_FAILED"
            message = f"録音開始失敗: {e}"
        _cleanup_session(session_id)
        _pipelines.pop(session_id, None)
        _delete_session_meta(session_id)
        _remove_empty_session_dir(session_id)
        raise conflict(code, message)
    if req.capture_system and not result.get("has_system_audio"):
        system_error = str(result.get("system_error") or "内部音声を開始できませんでした")
        try:
            await loop.run_in_executor(None, recorder.stop)
        except Exception as e:
            logger.warning("Failed to stop recorder after system audio start failure: %s", e)
        _cleanup_session(session_id)
        _pipelines.pop(session_id, None)
        _delete_session_meta(session_id)
        _remove_empty_session_dir(session_id)
        raise conflict("SYSTEM_AUDIO_START_FAILED", f"内部音声の録音開始失敗: {system_error}")

    _active_session_id = session_id
    _recording_paused = False
    _latest_level = 0.0
    _latest_system_level = 0.0
    result["project_id"] = req.project_id
    result["session_id"] = session_id
    result["transcription_deferred"] = bool(pipeline.get("transcription_deferred"))

    _level_task = asyncio.create_task(_stream_levels())
    await _broadcast_pipeline(session_id)

    return result


@router.post("/stop")
async def stop_recording() -> dict:
    global _level_task, _recording_paused
    sid = _active_session_id
    if sid is None:
        raise conflict("NOT_RECORDING", "録音中ではありません")

    existing_task = _finalize_tasks.get(sid)
    if existing_task is not None and not existing_task.done():
        return {"status": "stopping", "session_id": sid}
    if not recorder.is_recording:
        raise conflict("NOT_RECORDING", "録音を停止できません。状態を再取得してください")

    if _level_task:
        _level_task.cancel()
        _level_task = None

    _recording_paused = False
    task = asyncio.create_task(_finalize_session(sid))
    _finalize_tasks[sid] = task

    def _finalize_done(done: asyncio.Task, session_id: str = sid) -> None:
        if _finalize_tasks.get(session_id) is done:
            _finalize_tasks.pop(session_id, None)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Recording finalize task crashed [%s]", session_id)

    task.add_done_callback(_finalize_done)

    return {"status": "stopping", "session_id": sid}


@router.get("/status")
async def recording_status() -> dict:
    return {
        **recorder.get_status(),
        "session_id": _active_session_id,
        "paused": _recording_paused,
        "transcription_deferred": bool((_pipelines.get(_active_session_id) or {}).get("transcription_deferred")),
        "stopping": bool(
            _active_session_id
            and (_finalize_tasks.get(_active_session_id) is not None)
            and not _finalize_tasks[_active_session_id].done()
        ),
    }


@router.post("/pause")
async def set_recording_paused(body: dict) -> dict:
    """マイク・内部音声・ミックス音声を止め、録音時間から停止時間を除く。"""
    global _recording_paused
    if not recorder.is_recording or _active_session_id is None:
        raise conflict("NOT_RECORDING", "録音中ではありません")
    requested = bool(body.get("paused", False))
    sid = _active_session_id
    mixer = _mixers.get(sid)
    if requested:
        # Block the mixer immediately so samples already in flight cannot cross
        # the pause boundary while the capture processes are being stopped. The
        # public state stays unchanged until all capture processes acknowledge.
        if mixer is not None:
            mixer.pause()
    try:
        loop = asyncio.get_running_loop()
        actual_paused = await loop.run_in_executor(None, recorder.set_paused, requested)
    except Exception as error:
        _recording_paused = recorder.is_paused
        if not _recording_paused and mixer is not None:
            mixer.resume()
        raise conflict("RECORDING_PAUSE_FAILED", f"録音の一時停止状態を切り替えられませんでした: {error}") from error

    _recording_paused = actual_paused
    if not actual_paused and mixer is not None:
        mixer.resume()
    payload = {
        "state": "recording",
        "elapsed_sec": round(recorder.elapsed_sec, 1),
        "session_id": _active_session_id,
        "paused": _recording_paused,
    }
    await ws_manager.broadcast({"type": "recording_status", "data": payload})
    return {"status": "ok", "paused": _recording_paused, "elapsed_sec": payload["elapsed_sec"]}


@router.post("/mic-mute")
async def set_mic_mute(body: dict) -> dict:
    """マイクのソフトミュート ON/OFF。録音中だけ意味があるが、停止中でも 200 を返す。

    システム音声は影響を受けない。ミュート中は mic.wav に無音が記録され、
    文字起こし側にも 0 サンプルが渡される。
    """
    muted = bool(body.get("muted", False))
    recorder.set_mic_muted(muted)
    try:
        await ws_manager.broadcast({
            "type": "mic_mute_changed",
            "data": {"muted": muted, "session_id": _active_session_id},
        })
    except Exception:
        pass
    return {"status": "ok", "muted": muted}


@router.get("/microphones")
async def recording_microphones() -> dict:
    try:
        return await asyncio.to_thread(list_microphones)
    except Exception as error:
        logger.warning("Failed to list Core Audio microphones: %s", error)
        raise internal_error("MICROPHONE_LIST_FAILED", "マイクの一覧を取得できませんでした") from error


@router.put("/microphone")
async def switch_recording_microphone(request: MicrophoneRequest) -> dict:
    sid = _active_session_id
    if sid != request.session_id or not recorder.is_recording or sid in _finalize_tasks:
        raise conflict("NOT_RECORDING", "対象の録音は終了しています")
    try:
        return await asyncio.to_thread(recorder.switch_microphone, sid, request.device_uid)
    except Exception as error:
        logger.warning("Microphone switch failed [%s]: %s", sid, error)
        raise conflict("MICROPHONE_SWITCH_FAILED", f"マイクを切り替えられませんでした。{error}") from error


@router.get("/pipeline")
async def pipeline_status() -> dict:
    """直近 / 録音中の pipeline を返す(後方互換)。"""
    if _active_session_id and _active_session_id in _pipelines:
        return dict(_pipelines[_active_session_id])
    # 最新の pipeline(started_at 順)
    if not _pipelines:
        return {"state": "idle", "message": "", "result": None, "transcript": None, "error": None}
    latest = max(_pipelines.values(), key=lambda p: p.get("started_at", ""))
    return dict(latest)


@router.get("/pipelines")
async def pipelines_list() -> list[dict]:
    """全パイプラインを新しい順で返す。"""
    items = sorted(_pipelines.values(), key=lambda p: p.get("started_at", ""), reverse=True)
    return [dict(p) for p in items]


@router.delete("/pipelines/{session_id}")
async def pipelines_dismiss(session_id: str) -> dict:
    """完了/エラー済みの pipeline をリストから消す(UI 通知の解除用)。"""
    sid = _require_safe_session_id(session_id)
    p = _pipelines.get(sid)
    if p is None:
        raise not_found("PIPELINE_NOT_FOUND", f"session '{sid}' が見つかりません")
    if p.get("state") in ("recording", "stopping", "transcribing"):
        raise conflict("PIPELINE_ACTIVE", "実行中の pipeline は dismiss できません")
    _pipelines.pop(sid, None)
    return {"status": "dismissed", "session_id": sid}


@router.post("/pipeline/reset")
async def pipeline_reset() -> dict:
    """すべての完了済み pipeline を一括クリア(録音中のものは残す)。"""
    to_remove = [sid for sid, p in _pipelines.items()
                 if p.get("state") not in ("recording", "stopping", "transcribing")]
    for sid in to_remove:
        _pipelines.pop(sid, None)
    return {"status": "reset", "cleared": len(to_remove)}


@router.get("/last")
async def last_result() -> dict:
    if _last_result:
        return _last_result
    return {"error": "まだ録音がありません"}


@router.get("/recovery/status")
async def recovery_status() -> dict:
    """起動時のセッション復元の現在状態を返す (GUI 同期用)。"""
    return dict(_recovery_state)


@router.get("/play/{session_id}")
async def play_audio(session_id: str) -> FileResponse:
    from src.config import APP_DIR
    sid = _require_safe_session_id(session_id)
    session_dir = APP_DIR / "sessions" / sid
    audio = _pick_playback_audio(session_dir)
    if audio is not None:
        media_type = {
            ".mp3": "audio/mpeg",
            ".flac": "audio/flac",
            ".wav": "audio/wav",
        }.get(audio.suffix.lower(), "application/octet-stream")
        return FileResponse(str(audio), media_type=media_type)
    raise not_found("FILE_NOT_FOUND", f"音声ファイルが見つかりません: {sid}")


@router.get("/sessions/{session_id}/segments")
async def get_session_segments(session_id: str) -> list[dict]:
    """transcript.jsonl から確定済み segment を返す。

    DetailView を録音終了後/最中に開いた際の初期データ取得用。
    WS の transcript_chunk が来る前に既に確定していた segment を欠落させない。
    """
    sid = _require_safe_session_id(session_id)
    return _load_persisted_segments(sid)


@router.get("/sessions/{session_id}/audio_info")
async def get_session_audio_info(session_id: str) -> dict:
    """セッションの再生対象音声ファイルのメタ情報を返す。
    優先: combined.mp3 → combined.play.mp3 → 旧FLACの再生用MP3 → WAV系。
    """
    from src.config import APP_DIR
    sid = _require_safe_session_id(session_id)
    session_dir = APP_DIR / "sessions" / sid
    audio = _pick_playback_audio(session_dir)
    if audio is not None:
        return {
            "name": audio.name,
            "size_bytes": int(audio.stat().st_size),
        }
    raise not_found("FILE_NOT_FOUND", f"音声ファイルが見つかりません: {sid}")


@router.post("/sessions/{session_id}/recover")
async def recover_session_minutes(session_id: str, start_retranscribe: bool = False) -> dict:
    """セッション断片(音声/既存 transcript)から minutes を救済生成する。

    - 既に minutes が存在する場合はそれを返す (idempotent)
    - start_retranscribe=true かつ音声があれば、minutes 化後に再文字起こしを起動
    """
    sid = _require_safe_session_id(session_id)
    pipeline = _pipelines.get(sid) or {}
    prefer_project_id = pipeline.get("project_id")

    minutes, info = await _materialize_session_minutes(
        sid,
        prefer_project_id=str(prefer_project_id) if prefer_project_id else None,
        title_prefix="[救済] ",
    )

    # 既存エラー行を残し続けないよう、救済後は pipeline を done 側へ寄せる。
    p = _pipelines.get(sid)
    if p is not None and p.get("state") == "error":
        transcript = minutes.get("transcript") if isinstance(minutes.get("transcript"), list) else []
        p.update({
            "state": "done",
            "message": "救済保存済み",
            "error": None,
            "transcript": build_pipeline_transcript_payload(transcript),
        })
        _evict_completed_pipelines()
        await _broadcast_pipeline(sid)

    resp = {
        "status": "ok",
        "created": bool(info.get("created", False)),
        "used_audio": bool(info.get("used_audio", False)),
        "used_segments": bool(info.get("used_segments", False)),
        "minutes": minutes,
    }

    if start_retranscribe:
        if not info.get("used_audio", False):
            resp["retranscribe"] = {
                "status": "skipped",
                "reason": "audio_not_found",
            }
        else:
            from src.api import minutes as minutes_mod
            resp["retranscribe"] = await minutes_mod.retranscribe_minutes(minutes["id"])
    return resp


def _pick_recovery_audio(session_dir: Path) -> Path | None:
    return _pick_audio_file(session_dir, AUDIO_FILE_PRIORITY)


def _audio_duration_sec(audio: Path) -> int:
    try:
        if audio.suffix.lower() == ".flac":
            from src.audio.recorder import _flac_duration_sec
            return int(_flac_duration_sec(audio))
        import wave
        with wave.open(str(audio), "rb") as wf:
            rate = wf.getframerate()
            if rate <= 0:
                return 0
            return int(wf.getnframes() / rate)
    except Exception:
        return 0


def _parse_date_from_started_at(started_at: str | None) -> str:
    if started_at:
        try:
            date_str = str(started_at)[:10]
            datetime.strptime(date_str, "%Y-%m-%d")
            return date_str
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d")


def _guess_hhmm(session_id: str, started_at: str | None = None) -> str:
    if len(session_id) >= 13 and session_id[8] == "_":
        return f"{session_id[9:11]}:{session_id[11:13]}"
    if started_at and len(started_at) >= 16:
        try:
            return str(started_at)[11:16]
        except Exception:
            pass
    return "??:??"


async def _materialize_session_minutes(
    session_id: str,
    *,
    prefer_project_id: str | None = None,
    title_prefix: str = "[復元] ",
    enqueue_summary: bool = True,
    remove_meta: bool = True,
) -> tuple[dict, dict]:
    """session ディレクトリ上の音声/文字起こし断片から minutes を生成(または既存を返す)。"""
    from src.storage.db import db

    existing = db.get_minutes_by_session(session_id)
    if existing is not None:
        return existing, {
            "created": False,
            "used_audio": _pick_recovery_audio(_session_dir(session_id)) is not None,
            "used_segments": bool(existing.get("transcript")),
        }

    session_dir = _session_dir(session_id)
    if not session_dir.exists():
        raise not_found("SESSION_NOT_FOUND", f"セッションが見つかりません: {session_id}")

    meta: dict = {}
    meta_path = session_dir / SESSION_META_FILENAME
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    segments = _load_persisted_segments(session_id)
    audio_path = _pick_recovery_audio(session_dir)
    if not segments and audio_path is None:
        raise not_found("SESSION_DATA_NOT_FOUND", "音声と文字起こしデータが見つかりません")

    transcript: list[dict] = segments

    try:
        duration_sec = int(float(meta.get("duration_sec") or 0))
    except (TypeError, ValueError, OverflowError):
        duration_sec = 0
    if duration_sec <= 0 and audio_path:
        duration_sec = _audio_duration_sec(audio_path)
    if duration_sec <= 0 and transcript:
        try:
            duration_sec = int(max(float(seg.get("end", 0.0)) for seg in transcript))
        except Exception:
            duration_sec = 0

    pipeline = _pipelines.get(session_id) or {}
    started_at = (
        meta.get("started_at")
        or pipeline.get("started_at")
        or datetime.now(timezone.utc).isoformat()
    )
    date_str = _parse_date_from_started_at(started_at)
    hhmm = _guess_hhmm(session_id, started_at)

    minutes_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    project_id = (
        prefer_project_id
        or meta.get("project_id")
        or pipeline.get("project_id")
        or "default"
    )

    db.insert_minutes({
        "id": minutes_id,
        "session_id": session_id,
        "project_id": project_id,
        "title": f"{title_prefix}{hhmm} の会議",
        "date": date_str,
        "started_at": started_at,
        "duration_sec": int(duration_sec),
        "transcript": transcript,
        "summary": "",
        "llm_model": "",
        "created_at": now,
        "updated_at": now,
    })
    minutes = db.get_minutes(minutes_id)
    if minutes is None:
        raise RuntimeError("minutes insert failed")

    auto_summary_requested = (
        enqueue_summary
        and bool(transcript)
        and bool(config.get("minutes_ai", "auto_generate", default=True))
    )
    summary_queued = _enqueue_auto_summary(minutes_id) if auto_summary_requested else False
    if summary_queued:
        logger.info("[recover] auto-summary enqueued: %s", minutes_id)

    if remove_meta and (not auto_summary_requested or summary_queued):
        try:
            (session_dir / SESSION_META_FILENAME).unlink(missing_ok=True)
        except Exception:
            pass

    return minutes, {
        "created": True,
        "used_audio": audio_path is not None,
        "used_segments": bool(segments),
    }


def _run_recovery_coro_sync(coro):
    """同期 API 互換用: 復旧コルーチンを同期実行する。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("sync recovery API cannot run inside an active event loop")


def _iter_pending_recovery_metas() -> list[tuple[Path, dict]]:
    if not SESSIONS_DIR.exists():
        return []
    out: list[tuple[Path, dict]] = []
    for session_dir in sorted(SESSIONS_DIR.iterdir()):
        if not session_dir.is_dir():
            continue
        meta_path = session_dir / SESSION_META_FILENAME
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[recovery] failed to read meta %s: %s", session_dir, e)
            continue
        out.append((session_dir, meta))
    return out


def _recover_one_session(session_dir: Path, meta: dict) -> bool:
    """同期版互換 API。実体は async 実装を利用する。"""
    return _run_recovery_coro_sync(_recover_one_session_async(session_dir, meta))


async def _recover_one_session_async(session_dir: Path, meta: dict) -> bool:
    """1セッション分の救済処理(非同期版)。成功なら True / 対象外なら False。"""
    from src.storage.db import db

    session_id = meta.get("session_id") or session_dir.name
    state = meta.get("state")
    audio_path = _pick_recovery_audio(session_dir)
    existing = db.get_minutes_by_session(session_id)
    existing_segments = existing.get("transcript") if existing else None
    if isinstance(existing_segments, str):
        try:
            existing_segments = json.loads(existing_segments)
        except Exception:
            existing_segments = []

    retry_minutes_id = str(meta.get("retry_minutes_id") or "").strip()
    if retry_minutes_id:
        if existing is None or str(existing.get("id") or "") != retry_minutes_id:
            logger.warning(
                "[recovery] retry target missing for session %s (minutes_id=%s)",
                session_id,
                retry_minutes_id,
            )
            return False
        if state == "retry_saved":
            summary = str(existing.get("summary") or "").strip()
            auto_generate = bool(
                config.get("minutes_ai", "auto_generate", default=True)
            )
            if auto_generate and not summary:
                if not _enqueue_auto_summary(retry_minutes_id):
                    return False
                logger.info(
                    "[recovery] re-queued summary after retranscription: %s",
                    retry_minutes_id,
                )
            _delete_session_meta(session_id)
            return True

        try:
            from src.api.minutes import _start_retranscribe_minutes

            await _start_retranscribe_minutes(
                retry_minutes_id,
                enqueue_summary_on_complete=bool(
                    config.get("minutes_ai", "auto_generate", default=True)
                ),
            )
            logger.info(
                "[recovery] resumed interrupted retranscription: %s",
                retry_minutes_id,
            )
            return True
        except Exception as e:
            logger.warning(
                "[recovery] retranscription resume deferred (%s): %s",
                retry_minutes_id,
                e,
            )
            return False

    # DB insert 完了前に落ちたセッションは音声から文字起こしを再実行する。
    # 先に空の minutes を作り、処理に失敗しても音声と復元メタデータを残す。
    should_retranscribe = bool(audio_path) and (
        existing is None or not isinstance(existing_segments, list) or not existing_segments
    )
    if existing is not None and not should_retranscribe:
        if (
            isinstance(existing_segments, list)
            and existing_segments
            and not str(existing.get("summary") or "").strip()
            and bool(config.get("minutes_ai", "auto_generate", default=True))
        ):
            if not _enqueue_auto_summary(str(existing["id"])):
                return False
            logger.info(
                "[recovery] re-queued auto-summary for saved minutes: %s",
                existing["id"],
            )
            _delete_session_meta(session_id)
            return True
        _delete_session_meta(session_id)
        return False

    if existing is None:
        has_segments = bool(_load_persisted_segments(session_id))
        if not audio_path and not has_segments:
            logger.info(
                "[recovery] removing session metadata without recoverable data %s (state=%s)",
                session_id, state,
            )
            _delete_session_meta(session_id)
            return False

    try:
        if existing is not None:
            minutes = existing
            info = {
                "created": False,
                "used_audio": audio_path is not None,
                "used_segments": bool(existing_segments),
            }
        else:
            minutes, info = await _materialize_session_minutes(
                session_id,
                prefer_project_id=meta.get("project_id"),
                title_prefix="[復元] ",
                enqueue_summary=not should_retranscribe,
                remove_meta=not should_retranscribe,
            )

        if should_retranscribe:
            from src.api.minutes import _start_retranscribe_minutes

            await _start_retranscribe_minutes(
                str(minutes["id"]),
                enqueue_summary_on_complete=bool(
                    config.get("minutes_ai", "auto_generate", default=True)
                ),
            )
            logger.info("[recovery] full audio transcription queued: %s", session_id)
    except Exception as e:
        logger.warning("[recovery] retaining session %s for a later retry: %s", session_id, e)
        return False

    logger.info(
        "[recovery] recovered: %s (minutes_id=%s, prior_state=%s, used_audio=%s, used_segments=%s)",
        session_id,
        minutes.get("id"),
        state,
        info.get("used_audio"),
        info.get("used_segments"),
    )
    return bool(info.get("created", False) or should_retranscribe)


def recover_pending_sessions() -> int:
    """同期版互換 API。実体は async 実装を利用する。"""
    return _run_recovery_coro_sync(recover_pending_sessions_async())


async def recover_pending_sessions_async() -> int:
    """非同期版の起動時セッション復旧。

    DB 書き込みはメインスレッドで行い、音声変換などの重い処理だけ thread offload する。
    """
    metas = _iter_pending_recovery_metas()
    if not metas:
        _recovery_state.update({
            "state": "idle", "total": 0, "current": 0,
            "item": None, "recovered": 0,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        return 0

    total = len(metas)
    _recovery_state.update({
        "state": "running", "total": total, "current": 0,
        "item": None, "recovered": 0, "finished_at": None,
    })
    try:
        await ws_manager.broadcast({
            "type": "session_recovery_started",
            "data": {"total": total},
        })
    except Exception:
        pass

    recovered = 0
    for idx, (session_dir, meta) in enumerate(metas, start=1):
        session_id = meta.get("session_id") or session_dir.name
        started_at = meta.get("started_at")
        item = {
            "session_id": session_id,
            "started_at": started_at,
            "date": _parse_date_from_started_at(started_at),
            "hhmm": _guess_hhmm(session_id, started_at),
            "prior_state": meta.get("state"),
        }
        _recovery_state.update({"current": idx, "item": item})
        try:
            await ws_manager.broadcast({
                "type": "session_recovery_progress",
                "data": {"current": idx, "total": total, "item": item},
            })
        except Exception:
            pass
        try:
            if await _recover_one_session_async(session_dir, meta):
                recovered += 1
                _recovery_state["recovered"] = recovered
        except Exception as e:
            logger.error("[recovery] failed for %s: %s", session_dir.name, e)
        await asyncio.sleep(0)

    _recovery_state.update({
        "state": "done", "item": None,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        await ws_manager.broadcast({
            "type": "session_recovery_finished",
            "data": {"total": total, "recovered": recovered},
        })
    except Exception:
        pass

    if recovered:
        logger.info("[recovery] recovered %d pending session(s)", recovered)
    return recovered
