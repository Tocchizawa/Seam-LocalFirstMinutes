"""録音 + 文字起こしパイプラインのオーケストレーション。

複数パイプラインのキュー化に対応:
- 録音(マイク取得)は単一(ハードウェア制約)
- 録音終了後の文字起こし(streamer.flush)は **セッションごとの非同期タスク**
- 新しい録音は前のセッションが文字起こし中でも開始可能
- 各セッションは _pipelines に状態管理される
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.api.errors import bad_request, conflict, not_found
from src.api.ws import ws_manager
from src.audio.recorder import FFMPEG, SESSIONS_DIR, recorder
from src.audio.mixer import RealtimeMixer
from src.config import config
from src.project.manager import project_manager
from src.security import is_safe_session_id
from src.speakers import speaker_memory
from src.transcribe.streaming import StreamingTranscriber

logger = logging.getLogger(__name__)

SESSION_META_FILENAME = "session_meta.json"
TRANSCRIPT_JSONL_FILENAME = "transcript.jsonl"
_RECOVERABLE_STATES = {"stopping", "transcribing", "rediarizing", "saving"}
AUDIO_FILE_PRIORITY = ("combined.flac", "combined.wav", "system.wav", "mic.wav")
PLAYBACK_AUDIO_FILE_PRIORITY = ("combined.play.wav", "combined.wav", "combined.flac", "system.wav", "mic.wav")

router = APIRouter(prefix="/api/recording", tags=["recording"])


class StartRequest(BaseModel):
    project_id: str
    mic_device: int | None = None
    capture_system: bool = False


# ─── State ──────────────────────────────────────────────────────────────

# 録音のセッションは並行できないが、文字起こしは並行可。
# session_id → pipeline メタデータ
_pipelines: dict[str, dict] = {}
# 現在録音中の session_id(録音終了でクリア)
_active_session_id: str | None = None
# session ごとの streamer / mixer
_streamers: dict[str, StreamingTranscriber] = {}
_mixers: dict[str, RealtimeMixer] = {}

# audio_level 用(録音中のみ意味あり)
_level_task: asyncio.Task | None = None
_latest_level: float = 0.0       # マイク入力 RMS (0.0-1.0)
_latest_system_level: float = 0.0  # システム音声 RMS (0.0-1.0)
_last_result: dict | None = None

_ACTIVE_PIPELINE_STATES = {"recording", "stopping", "transcribing"}


def _watchdog_stall_sec() -> float:
    return float(config.get("debug", "watchdog_stall_sec", default=60.0))


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


def _ensure_playback_wav(flac_path: Path, wav_path: Path) -> Path | None:
    """FLAC 再生互換のため、必要時に WAV を生成して返す。

    Safari/WebView で FLAC seek が不安定なケースがあるため、
    再生系は WAV 優先にする。
    """
    try:
        if _is_valid_audio_file(wav_path):
            # 元 FLAC より古い場合だけ再生成
            if wav_path.stat().st_mtime >= flac_path.stat().st_mtime:
                return wav_path
        tmp_path = wav_path.with_suffix(".tmp.wav")
        cmd = [
            FFMPEG, "-y",
            "-i", str(flac_path),
            "-ar", "24000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            str(tmp_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            logger.warning(
                "playback wav generation failed for %s (code=%s): %s",
                flac_path.name,
                result.returncode,
                (result.stderr or "")[-400:],
            )
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        if not _is_valid_audio_file(tmp_path):
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        tmp_path.replace(wav_path)
        return wav_path
    except Exception as e:
        logger.warning("playback wav generation error: %s", e)
        return None


def _pick_playback_audio(session_dir: Path) -> Path | None:
    for name in PLAYBACK_AUDIO_FILE_PRIORITY:
        path = session_dir / name
        if not _is_valid_audio_file(path):
            continue
        if name == "combined.play.wav":
            flac_path = session_dir / "combined.flac"
            if _is_valid_audio_file(flac_path):
                refreshed = _ensure_playback_wav(flac_path, path)
                if refreshed is not None:
                    return refreshed
        if name == "combined.flac":
            wav_path = session_dir / "combined.play.wav"
            generated = _ensure_playback_wav(path, wav_path)
            if generated is not None:
                return generated
        return path
    return None


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
    active_streamer = _streamers.get(sid).get_debug_snapshot() if sid and sid in _streamers else None
    active_mixer = _mixers.get(sid).get_debug_snapshot() if sid and sid in _mixers else None

    sessions = []
    for session_id, p in sorted(_pipelines.items(), key=lambda x: x[1].get("started_at", ""), reverse=True):
        st = _streamers.get(session_id)
        mx = _mixers.get(session_id)
        sessions.append({
            "session_id": session_id,
            "state": p.get("state"),
            "project_id": p.get("project_id"),
            "started_at": p.get("started_at"),
            "streamer": st.get_debug_snapshot() if st else None,
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
        "pipelines_by_state": states,
        "active_pipeline": active_pipeline,
        "active_streamer": active_streamer,
        "active_mixer": active_mixer,
        "sessions": sessions,
    }


def _session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def _write_session_meta(session_id: str, **fields) -> None:
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
    except Exception as e:
        logger.warning("session_meta write failed [%s]: %s", session_id, e)


def _delete_session_meta(session_id: str) -> None:
    try:
        (_session_dir(session_id) / SESSION_META_FILENAME).unlink(missing_ok=True)
    except Exception:
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
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception as e:
        logger.warning("transcript jsonl read failed [%s]: %s", session_id, e)
    return out


def _new_pipeline(session_id: str, project_id: str) -> dict:
    return {
        "session_id": session_id,
        "project_id": project_id,
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
    return max(_latest_level, _latest_system_level)


async def _stream_levels() -> None:
    """録音中の WS 配信 + ワーカースレッド監視(watchdog) + 無音リマインド。"""
    import time as _time
    import psutil  # 遅延 import

    last_level = 0.0
    last_status = 0.0
    last_stream = 0.0
    last_watchdog = 0.0
    last_level_value = -1.0  # 初回は必ず送信
    reminder_enabled, reminder_silence_sec, reminder_level_threshold = _stop_forget_reminder_settings()
    silent_since: float | None = None
    reminded_in_current_silence = False
    # マイク無音早期検知: 録音開始後一定時間ずっと level=0 ならハードウェア/権限の異常
    started_at = _time.monotonic()
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

        # audio_level: 値変化が小さい時はスキップ
        if (now - last_level) >= 0.2 * scale:
            level_rounded = round(_latest_level, 3)
            if abs(level_rounded - last_level_value) >= 0.01 or last_level_value < 0:
                await ws_manager.broadcast({
                    "type": "audio_level",
                    "data": {"mic": level_rounded, "session_id": sid},
                })
                last_level_value = level_rounded
            last_level = now

            # 一度でも有効音声を検出したらフラグ立て (mic / system のどちらかでも OK)
            if not mic_seen_audio and _combined_level() >= MIC_AUDIO_THRESHOLD:
                mic_seen_audio = True

        # 録音開始から MIC_SILENT_WARN_SEC 経っても全く音を拾っていなければ警告 (1度だけ)
        # mic + system 両方とも無音だった場合のみ警告 (system audio 単独録音も正常扱い)
        if (
            not mic_silent_warned
            and not mic_seen_audio
            and sid
            and (now - started_at) >= MIC_SILENT_WARN_SEC
        ):
            mic_silent_warned = True
            mic_err = recorder.error if hasattr(recorder, "error") else None
            logger.warning(
                "[mic-silent] no audio detected (mic+system) for %.1fs (session=%s, error=%s)",
                now - started_at, sid, mic_err,
            )
            await ws_manager.broadcast({
                "type": "mic_silent_warning",
                "data": {
                    "session_id": sid,
                    "since_sec": int(now - started_at),
                    "error": mic_err,
                },
            })

        # recording_status: 1秒に1回(タイマー更新用)
        if (now - last_status) >= 1.0 * scale:
            await ws_manager.broadcast({
                "type": "recording_status",
                "data": {
                    "state": "recording",
                    "elapsed_sec": round(recorder.elapsed_sec, 1),
                    "session_id": sid,
                },
            })
            last_status = now

        # stop-forget reminder: 無音が一定時間続いたら通知(停止はしない)。
        # mic + system いずれも閾値未満のときだけ「無音」扱い。
        if reminder_enabled and sid:
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

        # watchdog: 5秒に1回ワーカースレッドの生死確認 + 死んでたら streamer 再起動
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
                    _set_state(sid, state="error", message=msg, error=msg)
                    await ws_manager.broadcast({
                        "type": "pipeline_error",
                        "data": {"session_id": sid, "message": msg},
                    })
                    await _broadcast_pipeline(sid)

                streamer = _streamers.get(sid)
                mixer = _mixers.get(sid)
                if mixer is not None and not mixer.consumer_alive:
                    logger.error("[watchdog] mixer consumer dead for %s", sid)
                    mixer.restart_consumer("watchdog dead")
                if streamer is not None:
                    if not streamer.worker_alive:
                        logger.error("[watchdog] streamer worker dead for %s — restarting", sid)
                        streamer.restart_worker("watchdog dead")
                    elif streamer.is_stalled(stall_sec=_watchdog_stall_sec()):
                        snap = streamer.get_debug_snapshot()
                        logger.warning(
                            "[watchdog] streamer stalled? queue=%d, pending=%.1fs, last_processed_age=%.1fs",
                            snap.get("queue_size", 0),
                            snap.get("pending_audio_sec", 0.0),
                            snap.get("last_processed_age_sec", -1.0) or -1.0,
                        )
                        # スタックしたワーカーが戻らないケースに備え、次世代 worker を立てる。
                        streamer.restart_worker("watchdog stalled", force=True)
            except Exception as e:
                logger.error("[watchdog] check failed: %s", e)
            last_watchdog = now

        # streaming_status: 1.5秒に1回
        if (now - last_stream) >= 1.5 * scale and sid and sid in _streamers:
            st = _streamers[sid]
            stream_data = st.get_debug_snapshot()
            stream_data["session_id"] = sid
            await ws_manager.broadcast({
                "type": "streaming_status",
                "data": stream_data,
            })
            last_stream = now

        await asyncio.sleep(0.1)


def _make_emit_segment(session_id: str):
    async def emit(segment: dict) -> None:
        _append_transcript_segment(session_id, segment)
        text_preview = (segment.get("text") or "")[:40]
        logger.info("emit_segment[%s] %s", session_id, text_preview)
        try:
            await ws_manager.broadcast({
                "type": "transcript_chunk",
                "data": {**segment, "session_id": session_id},
            })
        except Exception as e:
            logger.error("emit_segment failed [%s]: %s", session_id, e)
    return emit


async def _broadcast_pipeline(session_id: str) -> None:
    p = _pipelines.get(session_id)
    if p is None:
        return
    await ws_manager.broadcast({"type": "pipeline_progress", "data": dict(p)})


async def _finalize_session(session_id: str) -> None:
    """録音停止後の処理: mixer.stop + streamer.flush + DB 保存。
    録音中の他セッションがあっても並行して動く。"""
    p = _pipelines.get(session_id)
    if p is None:
        logger.error("finalize: session %s not found", session_id)
        return

    loop = asyncio.get_event_loop()
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

    # 1) recorder を停止して WAV を確定
    _set_state(session_id, state="stopping")
    _set_record_stage(Stage.STOPPING, "録音を停止中...")
    _write_session_meta(session_id, state="stopping")
    await _broadcast_pipeline(session_id)

    try:
        result = await loop.run_in_executor(None, recorder.stop)
        global _last_result, _active_session_id
        _last_result = result
        _set_state(session_id, result=result)

        if result.get("error"):
            _set_state(session_id, state="error", message=result["error"], error=result["error"])
            await ws_manager.broadcast({"type": "pipeline_error", "data": {"session_id": session_id, "message": result["error"]}})
            await _broadcast_pipeline(session_id)
            _cleanup_session(session_id)
            _active_session_id = None
            return

        await ws_manager.broadcast({
            "type": "recording_stopped",
            "data": {**result, "session_id": session_id},
        })
        # ここで recorder は free → 次の録音開始可能
        _active_session_id = None

    except Exception as e:
        logger.error("recorder.stop failed [%s]: %s", session_id, e)
        _set_state(session_id, state="error", message=str(e), error=str(e))
        await ws_manager.broadcast({"type": "pipeline_error", "data": {"session_id": session_id, "message": str(e)}})
        await _broadcast_pipeline(session_id)
        _cleanup_session(session_id)
        _active_session_id = None
        return

    # 2) Mixer を停止 → streamer flush
    _set_state(session_id, state="transcribing")
    _set_record_stage(Stage.WHISPER_FLUSH, "残りの文字起こしを処理中...")
    _write_session_meta(session_id, state="transcribing")
    await _broadcast_pipeline(session_id)

    try:
        mixer = _mixers.get(session_id)
        if mixer is not None:
            await loop.run_in_executor(None, mixer.stop)

        segments: list[dict] = []
        streamer_error: str | None = None
        streamer = _streamers.get(session_id)
        if streamer is not None:
            segments = await loop.run_in_executor(None, streamer.flush)
            streamer_error = streamer.model_error
            streamer.cleanup()

        # クリーンアップ
        _streamers.pop(session_id, None)
        _mixers.pop(session_id, None)

        if streamer_error and not segments:
            _set_state(session_id, state="error",
                       message=f"文字起こしに失敗: {streamer_error}",
                       error=streamer_error)
            await ws_manager.broadcast({"type": "pipeline_error",
                                        "data": {"session_id": session_id, "message": streamer_error}})
            await _broadcast_pipeline(session_id)
            return

        wav_path = None
        if isinstance(result, dict):
            wav_path = result.get("wav_path")
        _set_record_stage(Stage.DIARIZE_EXTRACT, "話者特徴を抽出中...")
        _write_session_meta(session_id, state="rediarizing", wav_path=wav_path)
        await _broadcast_pipeline(session_id)

        # 話者分離 (extract / cluster / assign) の進捗を WS に流す。
        # rediarize は worker thread で動くので run_coroutine_threadsafe 経由で broadcast。
        _last_diarize_emit = 0.0

        def _on_diarize_progress(stage_key: str, prog: float, message: str | None) -> None:
            nonlocal _last_diarize_emit
            import time as _t
            stage_enum = {
                "extract": Stage.DIARIZE_EXTRACT,
                "cluster": Stage.DIARIZE_CLUSTER,
                "assign": Stage.DIARIZE_CLUSTER,
            }.get(stage_key, Stage.DIARIZE_EXTRACT)
            _set_record_stage(stage_enum, message)
            p_state = _pipelines.get(session_id)
            if p_state is not None:
                p_state["progress"] = round(prog, 3)
            now_t = _t.monotonic()
            # 200ms throttle (但し進捗 1.0 完了時は必ず通知)
            if prog < 1.0 and (now_t - _last_diarize_emit) < 0.2:
                return
            _last_diarize_emit = now_t
            try:
                asyncio.run_coroutine_threadsafe(
                    _broadcast_pipeline(session_id), loop,
                )
            except Exception:
                pass

        transcript = await loop.run_in_executor(
            None,
            lambda: speaker_memory.rediarize_segments(
                segments,
                wav_path=wav_path,
                session_id=session_id,
                on_progress=_on_diarize_progress,
            ),
        )
        # rediarize 完了したので、progress フィールドをクリア (次の SAVING で再計算)
        if session_id in _pipelines:
            _pipelines[session_id]["progress"] = 1.0
        transcript_payload = build_pipeline_transcript_payload(transcript)

        _set_record_stage(Stage.SAVING, "DB に保存中...")
        _write_session_meta(session_id, state="saving")
        await _broadcast_pipeline(session_id)
        _set_state(session_id, state="done",
                   transcript=transcript_payload)

        # DB 保存 (insert 成功時のみ minutes_id を set。失敗時は None のまま)
        minutes_id: str | None = None
        try:
            from src.storage.db import db

            project_id = p["project_id"]
            duration = (p.get("result") or {}).get("duration_sec", 0)
            now = datetime.now(timezone.utc).isoformat()
            date_str = datetime.now().strftime("%Y-%m-%d")
            title = f"{datetime.now().strftime('%H:%M')} の会議"
            new_id = uuid.uuid4().hex[:12]

            db.insert_minutes({
                "id": new_id,
                "session_id": session_id,
                "project_id": project_id,
                "title": title,
                "date": date_str,
                "started_at": now,
                "duration_sec": int(duration),
                "transcript": transcript,
                "summary": "",
                "whisper_model": config.get("whisper", "model", default="medium"),
                "llm_model": "",
                "created_at": now,
                "updated_at": now,
            })
            minutes_id = new_id
            logger.info("Minutes saved: %s (%d segments)", session_id, len(transcript))
            _delete_session_meta(session_id)
        except Exception as e:
            logger.error("Failed to save minutes [%s]: %s", session_id, e)

        await ws_manager.broadcast({
            "type": "pipeline_done",
            "data": {"session_id": session_id, **transcript_payload},
        })
        await _broadcast_pipeline(session_id)

        # 自動要約トリガー (auto_generate=true 時のみ)
        auto_gen = bool(config.get("minutes_ai", "auto_generate", default=True))
        if minutes_id and auto_gen:
            try:
                from src.summarize.runner import get_runner

                get_runner().enqueue(minutes_id)
                logger.info("[summary] auto-summary enqueued: %s", minutes_id)
            except Exception as e:
                logger.warning("[summary] failed to enqueue auto-summary: %s", e)
        elif not auto_gen:
            logger.info(
                "[summary] auto_generate=false, skipping auto-summary for %s", session_id,
            )
        elif not minutes_id:
            logger.warning(
                "[summary] skipping auto-summary: minutes_id is None (DB save failed) for %s",
                session_id,
            )

    except Exception as e:
        logger.error("Streaming finalize failed [%s]: %s", session_id, e)
        _set_state(session_id, state="error", message=str(e), error=str(e))
        await ws_manager.broadcast({"type": "pipeline_error",
                                    "data": {"session_id": session_id, "message": str(e)}})
        await _broadcast_pipeline(session_id)
        _cleanup_session(session_id)


def _cleanup_session(session_id: str) -> None:
    """途中失敗時にリソース開放。"""
    streamer = _streamers.pop(session_id, None)
    if streamer is not None:
        try:
            streamer.cleanup()
        except Exception:
            pass
    mixer = _mixers.pop(session_id, None)
    if mixer is not None:
        try:
            mixer.stop()
        except Exception:
            pass


# ─── Endpoints ──────────────────────────────────────────────────────────

@router.post("/start")
async def start_recording(req: StartRequest) -> dict:
    global _active_session_id, _level_task, _latest_level, _latest_system_level
    if recorder.is_recording or _active_session_id is not None:
        raise conflict("ALREADY_RECORDING", "現在録音中です。停止してから新規録音してください")

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 衝突回避: 同秒に再開した場合は連番付与
    suffix = 0
    sid = session_id
    while sid in _pipelines:
        suffix += 1
        sid = f"{session_id}_{suffix}"
    session_id = sid

    pipeline = _new_pipeline(session_id, req.project_id)
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

    # プロジェクトの用語集を取得 (initial_prompt として whisper に渡す)
    project = project_manager.get(req.project_id)
    glossary = list(project.glossary) if project else []
    # corrections の正式表記 (correct 側) も Whisper の initial_prompt に混ぜ込む
    # これにより既知の誤転写を未然に防ぐ。
    corrections_list: list[tuple[str, str]] = []
    if project and project.corrections:
        for c in project.corrections:
            if c.correct and c.correct not in glossary:
                glossary.append(c.correct)
            if c.wrong and c.correct and c.wrong != c.correct:
                corrections_list.append((c.wrong, c.correct))

    # streamer + mixer 立ち上げ
    model_name = config.get("whisper", "model", default="medium")
    streaming_cfg = config.get("whisper", "streaming", default={}) or {}
    vad_provider = str(streaming_cfg.get("vad_provider", "silero")).lower()
    chunker_kwargs: dict = {
        "silence_duration_ms": streaming_cfg.get("silence_duration_ms", 500),
        "min_chunk_ms": streaming_cfg.get("min_chunk_ms", 1000),
        "max_chunk_ms": streaming_cfg.get("max_chunk_ms", 12000),
    }
    if vad_provider == "silero":
        chunker_kwargs["threshold"] = float(streaming_cfg.get("silero_threshold", 0.5))
    else:
        chunker_kwargs["rms_threshold"] = float(streaming_cfg.get("rms_threshold", 0.005))
    loop = asyncio.get_running_loop()
    model_load_timeout_sec = max(
        900.0,
        float(streaming_cfg.get("model_load_timeout_sec", 900.0)),
    )
    streamer = StreamingTranscriber(
        model_name=model_name,
        language=config.get("whisper", "language", default="ja"),
        on_segment=_make_emit_segment(session_id),
        session_id=session_id,
        chunker_kwargs=chunker_kwargs,
        # 録音中のバックプレッシャで欠落しないよう queue は無制限で運用する。
        max_queue_chunks=0,
        max_pending_audio_sec=max(30.0, float(streaming_cfg.get("max_pending_audio_sec", 240.0))),
        model_load_timeout_sec=model_load_timeout_sec,
        flush_join_timeout_sec=max(5.0, float(streaming_cfg.get("flush_join_timeout_sec", 120.0))),
        hallucination_cfg=config.get("whisper", "hallucination_filter", default={}) or {},
        glossary=glossary,
        corrections=corrections_list,
        vad_provider=vad_provider,
    )
    streamer.start(loop)
    mixer = RealtimeMixer(on_chunk=streamer.feed)
    mixer.start(has_system=req.capture_system)
    _streamers[session_id] = streamer
    _mixers[session_id] = mixer

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
        _cleanup_session(session_id)
        _pipelines.pop(session_id, None)
        _delete_session_meta(session_id)
        raise conflict("RECORDER_START_FAILED", f"録音開始失敗: {e}")

    _active_session_id = session_id
    _latest_level = 0.0
    _latest_system_level = 0.0
    result["project_id"] = req.project_id
    result["session_id"] = session_id

    _level_task = asyncio.create_task(_stream_levels())
    await _broadcast_pipeline(session_id)

    return result


@router.post("/stop")
async def stop_recording() -> dict:
    global _level_task
    if not recorder.is_recording or _active_session_id is None:
        raise conflict("NOT_RECORDING", "録音中ではありません")

    if _level_task:
        _level_task.cancel()
        _level_task = None

    sid = _active_session_id
    asyncio.create_task(_finalize_session(sid))

    return {"status": "stopping", "session_id": sid}


@router.get("/status")
async def recording_status() -> dict:
    return {**recorder.get_status(), "session_id": _active_session_id}


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


@router.get("/play/{session_id}")
async def play_audio(session_id: str) -> FileResponse:
    from src.config import APP_DIR
    sid = _require_safe_session_id(session_id)
    session_dir = APP_DIR / "sessions" / sid
    audio = _pick_playback_audio(session_dir)
    if audio is not None:
        media_type = "audio/flac" if audio.suffix.lower() == ".flac" else "audio/wav"
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
    優先: combined.play.wav → combined.wav → combined.flac → system.wav → mic.wav。
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


def _pick_recovery_wav(session_dir: Path) -> Path | None:
    for name in AUDIO_FILE_PRIORITY:
        wav = session_dir / name
        if wav.exists() and wav.stat().st_size > 44:
            return wav
    return None


def _wav_duration_sec(wav: Path) -> int:
    try:
        if wav.suffix.lower() == ".flac":
            from src.audio.recorder import _flac_duration_sec
            return int(_flac_duration_sec(wav))
        import wave
        with wave.open(str(wav), "rb") as wf:
            rate = wf.getframerate()
            if rate <= 0:
                return 0
            return int(wf.getnframes() / rate)
    except Exception:
        return 0


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

    if db.has_minutes_for_session(session_id):
        try:
            (session_dir / SESSION_META_FILENAME).unlink(missing_ok=True)
        except Exception:
            pass
        return False

    state = meta.get("state")
    if state not in _RECOVERABLE_STATES:
        logger.info(
            "[recovery] cleaning up unrecoverable session %s (state=%s)",
            session_id, state,
        )
        try:
            (session_dir / SESSION_META_FILENAME).unlink(missing_ok=True)
        except Exception as e:
            logger.warning("[recovery] failed to remove stale meta %s: %s",
                           session_id, e)
        return False

    segments = _load_persisted_segments(session_id)
    wav_path = _pick_recovery_wav(session_dir)

    if not segments and wav_path is None:
        logger.info("[recovery] skipping session %s (no transcript, no wav)", session_id)
        try:
            (session_dir / SESSION_META_FILENAME).unlink(missing_ok=True)
        except Exception:
            pass
        return False

    transcript: list[dict] = segments
    if segments and wav_path is not None:
        try:
            transcript = await asyncio.to_thread(
                speaker_memory.rediarize_segments,
                segments,
                wav_path=str(wav_path),
                session_id=session_id,
            )
        except Exception as e:
            logger.warning("[recovery] rediarize failed for %s, using raw segments: %s", session_id, e)
            transcript = segments

    duration_sec = _wav_duration_sec(wav_path) if wav_path else 0

    started_at = meta.get("started_at") or datetime.now(timezone.utc).isoformat()
    try:
        date_str = started_at[:10]
        datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        date_str = datetime.now().strftime("%Y-%m-%d")

    hhmm = "??:??"
    if len(session_id) >= 13 and session_id[8] == "_":
        hhmm = f"{session_id[9:11]}:{session_id[11:13]}"
    title = f"[復元] {hhmm} の会議"
    minutes_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()

    db.insert_minutes({
        "id": minutes_id,
        "session_id": session_id,
        "project_id": meta.get("project_id") or "default",
        "title": title,
        "date": date_str,
        "started_at": started_at,
        "duration_sec": int(duration_sec),
        "transcript": transcript,
        "summary": "",
        "whisper_model": config.get("whisper", "model", default="medium"),
        "llm_model": "",
        "created_at": now,
        "updated_at": now,
    })
    try:
        (session_dir / SESSION_META_FILENAME).unlink(missing_ok=True)
    except Exception:
        pass
    logger.info(
        "[recovery] recovered: %s (%d segments, wav=%s, prior_state=%s)",
        session_id, len(transcript), wav_path.name if wav_path else None, state,
    )

    if transcript and bool(config.get("minutes_ai", "auto_generate", default=True)):
        try:
            from src.summarize.runner import get_runner

            get_runner().enqueue(minutes_id)
            logger.info("[recovery] auto-summary enqueued for recovered: %s", minutes_id)
        except Exception as e:
            logger.warning(
                "[recovery] failed to enqueue auto-summary for %s: %s", minutes_id, e,
            )
    return True


def recover_pending_sessions() -> int:
    """同期版互換 API。実体は async 実装を利用する。"""
    return _run_recovery_coro_sync(recover_pending_sessions_async())


async def recover_pending_sessions_async() -> int:
    """非同期版の起動時セッション復旧。

    DB 書き込みはメインスレッドで行い、重い再話者分離だけ thread offload する。
    """
    recovered = 0
    for session_dir, meta in _iter_pending_recovery_metas():
        try:
            if await _recover_one_session_async(session_dir, meta):
                recovered += 1
        except Exception as e:
            logger.error("[recovery] failed for %s: %s", session_dir.name, e)
        await asyncio.sleep(0)

    if recovered:
        logger.info("[recovery] recovered %d pending session(s)", recovered)
    return recovered
