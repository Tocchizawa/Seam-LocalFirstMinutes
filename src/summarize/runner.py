"""要約ジョブの非同期キューとワーカー。

責務:
  - asyncio.Queue でジョブをシリアル消費 (concurrency=1)
  - in-memory に partial text を保持 (status API用)
  - 結果を DB に書き戻し + WS broadcast
  - 連打debounce: 同一minutes_id がin-flightなら既存をcancelして新ジョブ採用
  - draft保存: 生成成功後DB更新前に ~/.seam/summary_drafts/{id}.md に保存し、
    DB成功後に削除。失敗時はdraftが残り、次回起動時にリカバリ可

provider取得は遅延で行う (Ollama HTTPClientの作成等は worker内で)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from src.config import APP_DIR, config

from .base import (
    ProjectContext,
    SummarizerProvider,
    SummaryError,
    SummaryErrorCode,
    SummaryResult,
)
from .prompts import (
    build_messages,
    format_transcript_segments,
    is_too_short_for_summary,
)
from .registry import get_provider, is_known_provider

logger = logging.getLogger(__name__)

DRAFTS_DIR = APP_DIR / "summary_drafts"
SUMMARY_QUEUE_PATH = APP_DIR / "summary_queue.json"
CODEX_USAGE_PATH = APP_DIR / "codex_usage.jsonl"
MAX_RETAINED_SUMMARY_STATUSES = 100
DICTIONARY_UPDATE_TIMEOUT = 180
DICTIONARY_STAGE_LABELS = {
    "dictionary_waiting": "用語辞書の更新を待機中",
    "dictionary_reading": "参照資料を確認中",
    "dictionary_extracting": "用語・補正候補を抽出中",
    "dictionary_saving": "用語辞書を保存中",
}

_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _usage_int(item: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(item.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _record_codex_usage(minutes_id: str, model: str | None, usage: dict[str, int]) -> None:
    """Codexのusageだけを保存し、promptやCLI出力は保存しない。"""
    try:
        CODEX_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": time.time(),
            "minutes_id": minutes_id,
            "model": model,
            **{key: _usage_int(usage, key) for key in _USAGE_KEYS},
        }
        with CODEX_USAGE_PATH.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("[summary] failed to persist Codex usage: %s", exc)


def get_codex_usage_summary(days: int = 30) -> dict[str, Any]:
    days = max(1, min(365, int(days)))
    cutoff = time.time() - days * 24 * 60 * 60
    entries: list[dict[str, Any]] = []
    try:
        lines = CODEX_USAGE_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except Exception as exc:
        logger.warning("[summary] failed to read Codex usage: %s", exc)
        lines = []

    for line in lines:
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                continue
            timestamp = float(item.get("timestamp", 0))
        except (TypeError, ValueError):
            continue
        if timestamp < cutoff:
            continue
        entries.append(item)

    total = {
        key: sum(_usage_int(item, key) for item in entries)
        for key in _USAGE_KEYS
    }
    runs = len(entries)
    average = {key: round(value / runs) if runs else 0 for key, value in total.items()}
    recent = [
        {
            "timestamp": item.get("timestamp"),
            "model": item.get("model"),
            **{key: _usage_int(item, key) for key in _USAGE_KEYS},
        }
        for item in entries[-20:]
    ]
    return {"days": days, "runs": runs, "total": total, "average": average, "recent": recent}

JobState = Literal["queued", "running", "done", "failed", "cancelled", "skipped"]


@dataclass
class JobStatus:
    minutes_id: str
    state: JobState
    provider: str | None = None
    model: str | None = None
    partial_text: str = ""
    error_code: str | None = None
    error_message: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    # 細粒度ステージ (UI のステップ表示用)
    stage: str | None = None
    stage_label: str | None = None
    activity: str | None = None
    usage: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes_id": self.minutes_id,
            "state": self.state,
            "provider": self.provider,
            "model": self.model,
            "partial_text": self.partial_text,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "activity": self.activity,
            "usage": self.usage,
        }


@dataclass
class _Job:
    minutes_id: str
    provider_name: str | None     # None ならconfig既定
    enqueued_at: float
    status: JobStatus
    refresh: bool = False
    notes_changed: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


# WS broadcast用コールバック (型を緩めに)
WSBroadcaster = Callable[[dict[str, Any]], Awaitable[None]]


class SummaryRunner:
    """シリアル要約ジョブランナー。

    使い方:
        runner = SummaryRunner(broadcaster=ws_manager.broadcast)
        await runner.start()
        runner.enqueue("minutes_abc")
        ...
        await runner.shutdown()
    """

    def __init__(self, broadcaster: WSBroadcaster | None = None) -> None:
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._statuses: dict[str, JobStatus] = {}
        self._jobs: dict[str, _Job] = {}      # in-flight or queued
        self._durable_jobs: dict[str, str | None] = {}
        self._durable_refreshes: set[str] = set()
        self._durable_phase_b: dict[str, str | None] = {}
        self._current: _Job | None = None
        self._current_provider: SummarizerProvider | None = None
        self._phase_b_tasks: dict[str, asyncio.Task] = {}
        self._dictionary_stages: dict[str, str] = {}
        self._worker_task: asyncio.Task | None = None
        self._broadcaster = broadcaster
        self._shutdown = False

    # ─── lifecycle ────────────────────────────────────

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        self._restore_pending_jobs()
        self._worker_task = asyncio.create_task(
            self._worker(), name="summary-runner"
        )
        for minutes_id, provider_name in list(self._durable_phase_b.items()):
            self._schedule_phase_b(minutes_id, provider_name, persist=False)
        logger.info("SummaryRunner started")

    async def shutdown(self) -> None:
        self._shutdown = True
        if self._current is not None:
            self._current.cancel_event.set()
        if self._current_provider is not None:
            try:
                await self._current_provider.cancel(reason="shutdown")
            except Exception:
                pass
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None
        phase_b_tasks = list(self._phase_b_tasks.values())
        for task in phase_b_tasks:
            task.cancel()
        if phase_b_tasks:
            await asyncio.gather(*phase_b_tasks, return_exceptions=True)
        self._phase_b_tasks.clear()

    @staticmethod
    def _sync_queue_directory() -> None:
        """Rename/unlink 後のディレクトリエントリを可能なら同期する。"""
        try:
            directory_fd = os.open(SUMMARY_QUEUE_PATH.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            # 一部ファイルシステムではディレクトリ fsync が未対応。
            # キューファイル本体は fsync 済みで原子的に置換されている。
            logger.debug("[summary] queue directory sync unavailable: %s", exc)

    def _persist_pending_jobs(self) -> bool:
        """要約と辞書整理の待機ジョブを保存し、再起動時に戻す。"""
        temporary_path = SUMMARY_QUEUE_PATH.with_suffix(".json.tmp")
        try:
            if not self._durable_jobs and not self._durable_phase_b:
                existed = SUMMARY_QUEUE_PATH.exists()
                SUMMARY_QUEUE_PATH.unlink(missing_ok=True)
                if existed:
                    self._sync_queue_directory()
                return True
            SUMMARY_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
            jobs = [
                {
                    "minutes_id": minutes_id,
                    "provider": provider,
                    **({"refresh": True} if minutes_id in self._durable_refreshes else {}),
                }
                for minutes_id, provider in sorted(self._durable_jobs.items())
            ]
            phase_b = [
                {"minutes_id": minutes_id, "provider": provider}
                for minutes_id, provider in sorted(self._durable_phase_b.items())
            ]
            with temporary_path.open("w", encoding="utf-8") as queue_file:
                json.dump(
                    {"jobs": jobs, "phase_b": phase_b},
                    queue_file,
                    ensure_ascii=False,
                )
                queue_file.flush()
                os.fsync(queue_file.fileno())
            os.replace(temporary_path, SUMMARY_QUEUE_PATH)
            self._sync_queue_directory()
            return True
        except Exception as exc:
            logger.warning("[summary] failed to persist pending queue: %s", exc)
            try:
                temporary_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def _restore_pending_jobs(self) -> None:
        try:
            payload = json.loads(SUMMARY_QUEUE_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("[summary] pending queue could not be read: %s", exc)
            return

        raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        if not isinstance(raw_jobs, list):
            logger.warning("[summary] pending queue has an invalid jobs list")
            return

        from src.storage.db import db

        for item in raw_jobs:
            if not isinstance(item, dict):
                continue
            minutes_id = str(item.get("minutes_id") or "").strip()
            if not minutes_id or minutes_id in self._jobs:
                continue
            refresh = item.get("refresh") is True
            provider = item.get("provider")
            provider_name = str(provider) if provider else None
            try:
                record = db.get_minutes(minutes_id)
            except Exception as exc:
                logger.warning("[summary] queued minute lookup failed (%s): %s", minutes_id, exc)
                self._durable_jobs[minutes_id] = provider_name
                if refresh:
                    self._durable_refreshes.add(minutes_id)
                continue
            if record is None or (not refresh and str(record.get("summary") or "").strip()):
                continue
            if refresh:
                self._durable_refreshes.add(minutes_id)

            status = JobStatus(
                minutes_id=minutes_id,
                state="queued",
                provider=provider_name,
            )
            job = _Job(
                minutes_id=minutes_id,
                provider_name=provider_name,
                enqueued_at=time.time(),
                status=status,
                refresh=refresh,
            )
            self._durable_jobs[minutes_id] = provider_name
            self._jobs[minutes_id] = job
            self._statuses[minutes_id] = status
            self._queue.put_nowait(job)

        raw_phase_b = payload.get("phase_b", []) if isinstance(payload, dict) else []
        if isinstance(raw_phase_b, list):
            for item in raw_phase_b:
                if not isinstance(item, dict):
                    continue
                minutes_id = str(item.get("minutes_id") or "").strip()
                if not minutes_id or minutes_id in self._phase_b_tasks:
                    continue
                provider = item.get("provider")
                provider_name = str(provider) if provider else None
                self._durable_phase_b[minutes_id] = provider_name
                try:
                    record = db.get_minutes(minutes_id)
                except Exception as exc:
                    logger.warning(
                        "[summary] phase B minute lookup failed (%s): %s",
                        minutes_id,
                        exc,
                    )
                    continue
                if record is None or not str(record.get("summary") or "").strip():
                    self._durable_phase_b.pop(minutes_id, None)
                    continue
                self._schedule_phase_b(minutes_id, provider_name, persist=False)

        self._persist_pending_jobs()
        if self._durable_jobs:
            logger.info("[summary] restored %d pending job(s)", len(self._durable_jobs))
        if self._durable_phase_b:
            logger.info(
                "[summary] restored %d pending dictionary update(s)",
                len(self._durable_phase_b),
            )

    # ─── public API ────────────────────────────────────

    def enqueue(
        self, minutes_id: str, *, provider_name: str | None = None, refresh: bool = False
    ) -> bool:
        """要約ジョブを投入する。

        同一minutes_idの既存ジョブがあれば cancel して新ジョブで上書き。
        """
        previous_provider = self._durable_jobs.get(minutes_id)
        had_previous = minutes_id in self._durable_jobs
        was_refresh = minutes_id in self._durable_refreshes
        self._durable_jobs[minutes_id] = provider_name
        if refresh:
            self._durable_refreshes.add(minutes_id)
        else:
            self._durable_refreshes.discard(minutes_id)
        if not self._persist_pending_jobs():
            if was_refresh:
                self._durable_refreshes.add(minutes_id)
            else:
                self._durable_refreshes.discard(minutes_id)
            if had_previous:
                self._durable_jobs[minutes_id] = previous_provider
            else:
                self._durable_jobs.pop(minutes_id, None)
            logger.warning(
                "[summary] enqueue rejected because restart queue could not be saved: %s",
                minutes_id,
            )
            return False

        existing = self._jobs.get(minutes_id)
        if existing is not None:
            existing.cancel_event.set()
            if self._current is existing and self._current_provider is not None:
                try:
                    asyncio.create_task(self._current_provider.cancel())
                except RuntimeError:
                    pass
            logger.info(
                "[summary] cancelling existing job for re-enqueue: %s", minutes_id
            )

        status = JobStatus(
            minutes_id=minutes_id,
            state="queued",
            provider=provider_name,
        )
        job = _Job(
            minutes_id=minutes_id,
            provider_name=provider_name,
            enqueued_at=time.time(),
            status=status,
            refresh=refresh,
        )
        self._jobs[minutes_id] = job
        self._remember_status(status)
        self._queue.put_nowait(job)
        logger.info(
            "[summary] enqueued: %s (provider=%s)", minutes_id, provider_name or "default"
        )
        # キューに入った時点で UI に通知 (ワーカーがすぐ拾えない場合の即時フィードバック)
        if self._broadcaster is not None:
            try:
                import asyncio as _asyncio
                _asyncio.create_task(self._broadcast({
                    "type": "summary_stage",
                    "data": {
                        "minutes_id": minutes_id,
                        "stage": "queued",
                        "stage_label": "要約を待機中",
                        "provider": provider_name,
                    },
                }))
            except RuntimeError:
                # 起動時 (event loop 未稼働) は black hole 化、worker 開始後に再 broadcast される
                pass
        return True

    def refresh_for_notes(self, session_id: str) -> bool:
        from src.storage.db import db

        record = db.get_minutes_by_session(session_id)
        if not record:
            return True  # 録音中のメモは、停止後の初回要約で読み込む。
        minutes_id = record["id"]
        job = self._jobs.get(minutes_id)
        if job is not None and not job.cancel_event.is_set() and job.status.state in ("queued", "running", "done"):
            if job.status.state != "queued":
                job.notes_changed = True
            return True
        if not (record.get("summary") or "").strip():
            return True
        provider = (record.get("llm_model") or "").partition(":")[0]
        return self.enqueue(minutes_id, provider_name=provider if is_known_provider(provider) else None, refresh=True)

    def ensure_queued(self, minutes_id: str) -> bool:
        """実行中のジョブを止めずに、再起動用キューへの保存を確認する。"""
        status = self._statuses.get(minutes_id)
        if status is not None and status.state in ("queued", "running"):
            job = self._jobs.get(minutes_id)
            provider_name = job.provider_name if job is not None else status.provider
            self._durable_jobs[minutes_id] = provider_name
            return self._persist_pending_jobs()
        return self.enqueue(minutes_id)

    def pending_provider(self, minutes_id: str) -> str | None:
        """復元前でも、永続キューに記録された provider を取得する。"""
        for jobs in (self._durable_phase_b, self._durable_jobs):
            if minutes_id in jobs and jobs[minutes_id]:
                return jobs[minutes_id]
        try:
            payload = json.loads(SUMMARY_QUEUE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        for key in ("phase_b", "jobs"):
            entries = payload.get(key, [])
            if not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, dict) and str(item.get("minutes_id") or "") == minutes_id:
                    provider = item.get("provider")
                    if provider:
                        return str(provider)
        return None

    def persist_phase_b_intent(
        self, minutes_id: str, provider_name: str | None
    ) -> bool:
        """要約保存と辞書整理の間で落ちても後段処理を復元できるよう先に記録する。"""
        self._durable_phase_b[minutes_id] = provider_name
        return self._persist_pending_jobs()

    def _remember_status(self, status: JobStatus) -> None:
        self._statuses.pop(status.minutes_id, None)
        self._statuses[status.minutes_id] = status
        self._prune_statuses()

    def _prune_statuses(self) -> None:
        excess = sum(
            1
            for status in self._statuses.values()
            if status.state not in ("queued", "running")
        ) - MAX_RETAINED_SUMMARY_STATUSES
        if excess <= 0:
            return
        for minutes_id, status in list(self._statuses.items()):
            if status.state in ("queued", "running"):
                continue
            self._statuses.pop(minutes_id, None)
            excess -= 1
            if excess <= 0:
                return

    def get_status(self, minutes_id: str) -> JobStatus | None:
        return self._statuses.get(minutes_id)

    def is_busy(self, minutes_id: str) -> bool:
        status = self._statuses.get(minutes_id)
        if status is not None and status.state in ("queued", "running"):
            return True
        return self.is_phase_b_busy(minutes_id)

    def is_phase_b_busy(self, minutes_id: str) -> bool:
        task = self._phase_b_tasks.get(minutes_id)
        return task is not None and not task.done()

    def list_active_phase_b(self) -> list[str]:
        """辞書整理中の議事録 ID 一覧を返す。"""
        return [
            minutes_id
            for minutes_id, task in self._phase_b_tasks.items()
            if not task.done()
        ]

    def dictionary_stage(self, minutes_id: str) -> tuple[str, str] | None:
        stage = self._dictionary_stages.get(minutes_id)
        return (stage, DICTIONARY_STAGE_LABELS[stage]) if stage is not None else None

    def list_active(self) -> list[str]:
        """進行中 (queued / running) の minutes_id 一覧を返す。"""
        active: list[str] = []
        for mid, status in self._statuses.items():
            if status.state in ("queued", "running"):
                active.append(mid)
        return active

    async def cancel(self, minutes_id: str) -> bool:
        """指定ジョブをキャンセルする。

        Returns:
            キャンセル可能だった場合 True
        """
        job = self._jobs.get(minutes_id)
        phase_b = self._phase_b_tasks.get(minutes_id)
        if phase_b is not None and (phase_b.done() or (job and job.status.state in ("queued", "running"))):
            phase_b = None
        if phase_b is None and job is None:
            return False

        pending = self._durable_jobs
        if phase_b is not None:
            pending = self._durable_phase_b
            # A draft left by recovery would otherwise enqueue dictionary work again.
            (DRAFTS_DIR / f"{minutes_id}.md").unlink(missing_ok=True)
        if minutes_id in pending:
            provider_name = pending.pop(minutes_id)
            if not self._persist_pending_jobs():
                pending[minutes_id] = provider_name
                raise OSError("summary cancellation could not be persisted")
            if pending is self._durable_jobs:
                self._durable_refreshes.discard(minutes_id)

        if phase_b is not None:
            phase_b.cancel()
            await asyncio.gather(phase_b, return_exceptions=True)
            await self._broadcast({
                "type": "dictionary_update_finished",
                "data": {"minutes_id": minutes_id},
            })
            return True

        assert job is not None
        job.cancel_event.set()
        if job.status.state == "queued":
            self._jobs.pop(minutes_id, None)
            job.status.state = "cancelled"
            job.status.finished_at = time.time()
            self._remember_status(job.status)
            await self._broadcast({
                "type": "summary_cancelled",
                "data": {"minutes_id": minutes_id},
            })
        # 進行中ジョブだったら provider 側もcancel
        if self._current is job:
            if self._current_provider is not None:
                try:
                    await self._current_provider.cancel()
                except Exception as e:
                    logger.warning("provider cancel raised: %s", e)
        return True

    # ─── worker ──────────────────────────────────────

    async def _worker(self) -> None:
        """シリアル消費。1ジョブずつ処理。"""
        while not self._shutdown:
            try:
                job = await self._queue.get()
            except asyncio.CancelledError:
                return

            # キャンセル済みジョブはスキップ
            if job.cancel_event.is_set():
                if self._jobs.get(job.minutes_id) is job:
                    self._jobs.pop(job.minutes_id, None)
                    if not self._shutdown:
                        self._durable_jobs.pop(job.minutes_id, None)
                        self._durable_refreshes.discard(job.minutes_id)
                        self._persist_pending_jobs()
                    job.status.state = "cancelled"
                    job.status.finished_at = time.time()
                    self._remember_status(job.status)
                continue

            self._current = job
            local_model_lease = False
            dequeued_at = time.monotonic()
            queue_wait = max(0, time.time() - job.enqueued_at)
            try:
                provider_name = job.provider_name or str(
                    (config.get("minutes_ai") or {}).get("provider", "ollama")
                )
                if provider_name in {"ollama", "claude_code", "codex"}:
                    from src.audio.resource_monitor import model_resource_gate

                    local_model_lease = await model_resource_gate.acquire_llm_async(
                        job.cancel_event.is_set,
                    )
                    if not local_model_lease:
                        if self._jobs.get(job.minutes_id) is job:
                            job.status.state = "cancelled"
                            job.status.finished_at = time.time()
                            self._remember_status(job.status)
                            await self._broadcast({
                                "type": "summary_cancelled",
                                "data": {"minutes_id": job.minutes_id},
                            })
                        continue
                if self._jobs.get(job.minutes_id) is job and not job.cancel_event.is_set():
                    logger.info(
                        "[summary] start %s provider=%s queue_wait=%.1fs resource_wait=%.1fs",
                        job.minutes_id, provider_name, queue_wait,
                        time.monotonic() - dequeued_at,
                    )
                    await self._process_job(job)
                elif self._jobs.get(job.minutes_id) is job:
                    job.status.state = "cancelled"
                    job.status.finished_at = time.time()
                    self._remember_status(job.status)
            except Exception as e:
                logger.exception("[summary] worker unhandled error: %s", e)
                if (
                    self._jobs.get(job.minutes_id) is job
                    and not job.cancel_event.is_set()
                    and job.status.state in ("queued", "running")
                ):
                    self._mark_failed(
                        job.status,
                        SummaryErrorCode.UNKNOWN.value,
                        f"要約処理に失敗しました: {e}",
                    )
                    await self._broadcast_failed(job.status, job)
            finally:
                if local_model_lease:
                    from src.audio.resource_monitor import model_resource_gate

                    model_resource_gate.release_llm()
                self._current = None
                self._current_provider = None
                if self._jobs.get(job.minutes_id) is job:
                    self._jobs.pop(job.minutes_id, None)
                    if not self._shutdown:
                        self._durable_jobs.pop(job.minutes_id, None)
                        self._durable_refreshes.discard(job.minutes_id)
                        self._persist_pending_jobs()
                if self._statuses.get(job.minutes_id) is job.status:
                    self._remember_status(job.status)
                if not self._shutdown and not job.cancel_event.is_set() and job.notes_changed:
                    from src.storage.db import db

                    record = db.get_minutes(job.minutes_id, include_transcript=False)
                    if record:
                        self.refresh_for_notes(record["session_id"])

    async def _process_job(self, job: _Job) -> None:
        from src.storage.db import db
        from src.project.manager import project_manager
        from src.pipeline.state import Stage, STAGE_LABELS

        minutes_id = job.minutes_id
        status = job.status
        if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
            return
        status.state = "running"
        status.started_at = time.time()
        status.stage = Stage.SUMMARY_HEALTH.value
        status.stage_label = STAGE_LABELS[Stage.SUMMARY_HEALTH]
        await self._broadcast({
            "type": "summary_stage",
            "data": {
                "minutes_id": minutes_id,
                "stage": status.stage,
                "stage_label": status.stage_label,
            },
        })
        if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
            return

        # 1. DBから minutes 取得
        record = db.get_minutes(minutes_id)
        if record is None:
            logger.warning("[summary] minutes not found: %s", minutes_id)
            self._mark_failed(
                status,
                SummaryErrorCode.NOT_CONFIGURED.value,
                "minutes が見つかりません",
            )
            return

        transcript_segments = record.get("transcript")
        if isinstance(transcript_segments, str):
            try:
                transcript_segments = json.loads(transcript_segments)
            except Exception:
                transcript_segments = []
        if not isinstance(transcript_segments, list):
            transcript_segments = []

        transcript_text = format_transcript_segments(transcript_segments)

        # 2. 文字起こし極小→スキップ
        previous = str(record.get("summary") or "").strip() if job.refresh else ""
        if (is_too_short_for_summary(transcript_text) and not previous
                and not db.meeting_notes(record["session_id"])):
            logger.info("[summary] transcript too short, skipping: %s", minutes_id)
            status.state = "skipped"
            status.finished_at = time.time()
            if self._jobs.get(minutes_id) is job:
                await self._broadcast({
                    "type": "summary_skipped",
                    "data": {"minutes_id": minutes_id, "reason": "TRANSCRIPT_TOO_SHORT"},
                })
            return

        # 3. provider 選択
        ai_cfg = config.get("minutes_ai") or {}
        provider_name = job.provider_name or str(ai_cfg.get("provider", "ollama"))
        status.provider = provider_name

        # 4. cloud provider なら同意フラグチェック
        from .registry import is_cloud_provider

        if is_cloud_provider(provider_name):
            consent_map = ai_cfg.get("consent") or {}
            if not bool(consent_map.get(provider_name)):
                self._mark_failed(
                    status,
                    SummaryErrorCode.NOT_CONFIGURED.value,
                    f"{provider_name} の利用同意が未取得です。設定画面で同意してください。",
                )
                await self._broadcast_failed(status, job)
                return

        # 5. provider取得
        try:
            provider = get_provider(provider_name, ai_cfg)
        except SummaryError as e:
            self._mark_failed(status, e.code.value, e.message)
            await self._broadcast_failed(status, job)
            return

        # 6. health check
        try:
            health = await provider.health_check()
            if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
                return
            if not health.ok:
                self._mark_failed(status, health.code, health.message)
                await self._broadcast_failed(status, job)
                return
            status.model = health.model
        except Exception as e:
            logger.exception("[summary] health_check failed: %s", e)
            self._mark_failed(
                status, SummaryErrorCode.UNKNOWN.value, str(e)
            )
            await self._broadcast_failed(status, job)
            return

        # 7. ProjectContext 構築
        project = build_project_context(record.get("project_id"), project_manager)

        # 8. 生成
        self._current_provider = provider
        timeout_sec = float(ai_cfg.get("timeout_sec", 300))
        status.stage = Stage.SUMMARY_GENERATE.value
        status.stage_label = STAGE_LABELS[Stage.SUMMARY_GENERATE]
        await self._broadcast({
            "type": "summary_stage",
            "data": {
                "minutes_id": minutes_id,
                "stage": status.stage,
                "stage_label": status.stage_label,
            },
        })
        if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
            return

        async def on_token(chunk: str) -> None:
            if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
                return
            status.partial_text += chunk
            await self._broadcast({
                "type": "summary_chunk",
                "data": {
                    "minutes_id": minutes_id,
                    "text": chunk,
                    "total_chars": len(status.partial_text),
                },
            })

        async def on_activity(label: str) -> None:
            # CLI provider (claude_code / codex) が「何をしているか」を UI に伝えるイベント
            if not label or self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
                return
            status.activity = label
            await self._broadcast({
                "type": "summary_activity",
                "data": {
                    "minutes_id": minutes_id,
                    "activity": label,
                },
            })

        try:
            if job.cancel_event.is_set():
                raise SummaryError(
                    SummaryErrorCode.CANCELLED, "ジョブがキャンセルされました"
                )
            job.notes_changed = False
            notes = [
                {"id": note["id"], "start": note["start"], "title": note["title"], "memo": note["body"]}
                for note in db.meeting_notes(record["session_id"])
            ]
            note_context = json.dumps(notes, ensure_ascii=False)
            if notes:
                transcript_text += (
                    "\n\n# 発言に付けられたユーザーメモ (JSON)\n"
                    "start は録音開始からの秒数。対応する発言と合わせて補足情報として考慮してください。\n"
                    + note_context
                )

            system, user = build_messages(transcript_text, project)
            context = db.summary_context(minutes_id) if previous else {}
            options: dict[str, Any] = {}
            if getattr(provider, "supports_resume", False):
                options["persist_session"] = True
            if previous:
                system = context.get("system") or system
                user = context.get("user") or user
                update = (
                    "発言に紐づくユーザーメモが更新されました。以下は現在有効なメモの全件です。"
                    "一覧から削除されたメモにのみ由来する内容は取り除き、文字起こしに根拠がある記述は保持してください。"
                    "直前の要約を更新し、差分や説明を付けず、同じ形式で更新後の要約全文を返してください。"
                    f"\n\n# 現在のメモ (JSON)\n{note_context}"
                )
                if context.get("session_id"):
                    update += f"\n\n# 現在の要約\n{previous}"
                options["messages"] = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": previous},
                    {"role": "user", "content": update},
                ]
                if options.get("persist_session") and context.get("session_id"):
                    options["resume_session"] = context["session_id"]

            async def generate() -> SummaryResult:
                return await provider.generate(
                    transcript_text,
                    project=project,
                    on_token=on_token,
                    on_activity=on_activity,
                    timeout_sec=timeout_sec,
                    **options,
                )

            try:
                result = await generate()
            except SummaryError as error:
                # CLI側で古いセッションが消えていても、Seamに残した会話から継続できる。
                if not options.get("resume_session") or error.code not in (
                    SummaryErrorCode.UNKNOWN, SummaryErrorCode.NOT_CONFIGURED,
                ) or job.cancel_event.is_set():
                    raise
                options.pop("resume_session")
                status.partial_text = ""
                result = await generate()
            if not result.text.strip():
                raise SummaryError(SummaryErrorCode.OUTPUT_FORMAT, "要約が空でした")
            summary_context = {"system": system, "user": user, "session_id": result.session_id}
        except SummaryError as e:
            if e.code == SummaryErrorCode.CANCELLED or job.cancel_event.is_set():
                status.state = "cancelled"
                status.finished_at = time.time()
                if self._jobs.get(minutes_id) is job:
                    await self._broadcast({
                        "type": "summary_cancelled",
                        "data": {"minutes_id": minutes_id},
                    })
                return
            self._mark_failed(status, e.code.value, e.message)
            await self._broadcast_failed(status, job)
            return
        except asyncio.CancelledError:
            status.state = "cancelled"
            status.finished_at = time.time()
            if self._jobs.get(minutes_id) is job:
                await self._broadcast({
                    "type": "summary_cancelled",
                    "data": {"minutes_id": minutes_id},
                })
            return
        except Exception as e:
            logger.exception("[summary] generate raised unexpected: %s", e)
            self._mark_failed(
                status, SummaryErrorCode.UNKNOWN.value, str(e)
            )
            await self._broadcast_failed(status, job)
            return

        if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
            return

        if result.usage:
            status.usage = result.usage
            await self._broadcast({
                "type": "summary_usage",
                "data": {
                    "minutes_id": minutes_id,
                    "provider": result.provider,
                    "model": result.model,
                    "usage": result.usage,
                },
            })
            if result.provider == "codex":
                _record_codex_usage(minutes_id, result.model, result.usage)

        # 9. draft保存 → DB更新 → draft削除
        await self._save_with_recovery(record, status, result, job, context=summary_context)

    def _mark_failed(
        self, status: JobStatus, code: str, message: str
    ) -> None:
        status.state = "failed"
        status.error_code = code
        status.error_message = message
        status.finished_at = time.time()
        logger.warning(
            "[summary] failed %s: %s — %s", status.minutes_id, code, message
        )

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if self._broadcaster is None:
            return
        try:
            await self._broadcaster(payload)
        except Exception as e:
            logger.warning("WS broadcast failed: %s", e)

    async def _broadcast_failed(self, status: JobStatus, job: _Job | None = None) -> None:
        if job is not None and self._jobs.get(job.minutes_id) is not job:
            return
        await self._broadcast({
            "type": "summary_failed",
            "data": {
                "minutes_id": status.minutes_id,
                "error_code": status.error_code,
                "message": status.error_message,
            },
        })

    async def _save_with_recovery(
        self, record: dict, status: JobStatus, result: SummaryResult, job: _Job,
        *, context: dict | None = None,
    ) -> None:
        """要約結果を draft → DB → draft削除 の順で保存。

        DB更新失敗時、draft が残るのでアプリ起動時の recover_drafts でリカバリ可。
        """
        from src.storage.db import db
        from src.pipeline.state import Stage, STAGE_LABELS

        minutes_id = status.minutes_id
        if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
            return
        status.stage = Stage.SUMMARY_SAVE.value
        status.stage_label = STAGE_LABELS[Stage.SUMMARY_SAVE]
        await self._broadcast({
            "type": "summary_stage",
            "data": {
                "minutes_id": minutes_id,
                "stage": status.stage,
                "stage_label": status.stage_label,
            },
        })
        if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
            return

        draft_path = DRAFTS_DIR / f"{minutes_id}.md"
        phase_b_persisted = False
        if not job.refresh:
            DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

            # 要約本体 (タイトルセクション無し、概要〜議論ハイライトのみ)
            temporary_draft_path = draft_path.with_suffix(".md.tmp")
            try:
                with temporary_draft_path.open("w", encoding="utf-8") as draft_file:
                    draft_file.write(result.text)
                    draft_file.flush()
                    os.fsync(draft_file.fileno())
                os.replace(temporary_draft_path, draft_path)
            except Exception as e:
                logger.warning("[summary] failed to write draft (%s): %s", minutes_id, e)
                try:
                    temporary_draft_path.unlink(missing_ok=True)
                except Exception:
                    pass

            # DB保存より先に後段ジョブを記録する。起動時には要約が空ならこの
            # intent を捨てて要約ジョブを再開し、要約があれば辞書整理を再開する。
            phase_b_persisted = self.persist_phase_b_intent(
                minutes_id, result.provider
            )

        new_title: str | None = None
        try:
            saved = db.update_summary(
                minutes_id,
                result.text,
                llm_model=f"{result.provider}:{result.model}",
                context=context,
            )
        except Exception as e:
            logger.exception("[summary] DB update failed: %s", e)
            if not job.refresh:
                self._durable_phase_b.pop(minutes_id, None)
                self._persist_pending_jobs()
            self._mark_failed(
                status, SummaryErrorCode.UNKNOWN.value, f"DB更新に失敗: {e}"
            )
            await self._broadcast_failed(status, job)
            return

        if not saved:
            if not job.refresh:
                self._durable_phase_b.pop(minutes_id, None)
                self._persist_pending_jobs()
            # The minute may have been deleted while generation was running.
            # Do not report success for a result that was never persisted.
            try:
                draft_path.unlink(missing_ok=True)
            except Exception:
                pass
            self._mark_failed(
                status, SummaryErrorCode.NOT_CONFIGURED.value,
                "議事録が削除されたため、要約を保存できませんでした",
            )
            await self._broadcast_failed(status, job)
            return

        # 要約DB更新後に永続化を再確認する。保存に失敗した場合は下書きを残し、
        # 起動時に要約が既に存在すれば辞書整理の復元マーカーとして利用する。
        if not job.refresh and not phase_b_persisted:
            phase_b_persisted = self._persist_pending_jobs()
        if phase_b_persisted:
            try:
                draft_path.unlink(missing_ok=True)
            except Exception:
                pass

        # ─── タイトル生成 (別リクエスト、失敗しても要約結果は保持) ───
        from src.config import config as _cfg

        ai_cfg = _cfg.get("minutes_ai") or {}
        title_enabled = bool(ai_cfg.get("generate_title", True))
        if title_enabled and not job.refresh:
            try:
                # 復元用: 大半の provider に generate_title が実装されてなければ skip
                provider_obj = self._current_provider
                if provider_obj is not None and hasattr(provider_obj, "generate_title"):
                    # transcript を取得 (要約が呼ばれた時のものを再利用)
                    record_now = db.get_minutes(status.minutes_id)
                    transcript_for_title = ""
                    if record_now:
                        seg_raw = record_now.get("transcript")
                        if isinstance(seg_raw, str):
                            try:
                                seg_raw = json.loads(seg_raw)
                            except Exception:
                                seg_raw = []
                        from .prompts import (
                            format_transcript_segments,
                            truncate_transcript_for_title,
                        )
                        transcript_for_title = truncate_transcript_for_title(
                            format_transcript_segments(seg_raw or []),
                        )

                    project_now = build_project_context(
                        record_now.get("project_id") if record_now else None,
                        __import__("src.project.manager", fromlist=["project_manager"]).project_manager,
                    )
                    raw_title = await provider_obj.generate_title(
                        transcript_for_title,
                        project=project_now,
                        timeout_sec=60,
                    )
                    if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
                        return
                    from .prompts import normalize_generated_title

                    new_title = normalize_generated_title(raw_title)
                    if new_title:
                        try:
                            db.update_title(status.minutes_id, new_title)
                        except Exception as e:
                            logger.warning(
                                "[summary] update_title failed: %s", e,
                            )
                            new_title = None
            except Exception as e:
                logger.warning("[summary] title generation failed: %s", e)
                new_title = None

        # ─── 要約完了を即時ブロードキャスト ─────────────────
        # Phase B (辞書整理) は CLI provider だと 1〜2 分かかるので、
        # ここで done を立てて UI を解放してから、Phase B はバックグラウンドで走らせる。
        status.state = "done"
        status.partial_text = result.text
        status.model = result.model
        status.finished_at = time.time()
        if self._jobs.get(minutes_id) is not job or job.cancel_event.is_set():
            return
        await self._broadcast({
            "type": "summary_done",
            "data": {
                "minutes_id": minutes_id,
                "provider": result.provider,
                "model": result.model,
                "duration_sec": result.duration_sec,
                "input_chars": result.input_chars,
                "output_chars": result.output_chars,
                "usage": result.usage,
                # タイトル生成成功時のみ含む。フロントは値があれば minutes.title を上書き表示する。
                "new_title": new_title,
            },
        })
        if new_title and self._jobs.get(minutes_id) is job:
            # 一覧画面など別箇所も再描画できるよう、専用イベントも送る
            await self._broadcast({
                "type": "minutes_title_updated",
                "data": {
                    "minutes_id": minutes_id,
                    "title": new_title,
                },
            })
        logger.info(
            "[summary] done %s (%s/%s, %.1fs, %d chars)",
            minutes_id, result.provider, result.model,
            result.duration_sec, result.output_chars,
        )

        # ─── 辞書整理 (Phase B) をバックグラウンドで実行 ───
        # transcript と glossary/docs を LLM で照合し、新規 glossary + 誤転写ペアを発見。
        # ここを await しないことで、UI への "summary_done" は即時届く。
        minutes_id = status.minutes_id
        if job.refresh:
            return
        previous_phase_b = self._phase_b_tasks.get(minutes_id)
        if previous_phase_b is not None and not previous_phase_b.done():
            return
        self._schedule_phase_b(minutes_id, result.provider, persist=False)

    def _schedule_phase_b(
        self,
        minutes_id: str,
        provider_name: str | None,
        *,
        persist: bool = True,
    ) -> None:
        previous = self._phase_b_tasks.get(minutes_id)
        if previous is not None and not previous.done():
            return
        self._durable_phase_b[minutes_id] = provider_name
        if persist:
            self._persist_pending_jobs()
        task = asyncio.create_task(
            self._run_durable_phase_b(minutes_id, provider_name),
            name=f"summary-phase-b-{minutes_id}",
        )
        self._dictionary_stages[minutes_id] = "dictionary_waiting"
        self._phase_b_tasks[minutes_id] = task

        def clear_finished_task(done: asyncio.Task) -> None:
            if self._phase_b_tasks.get(minutes_id) is done:
                self._phase_b_tasks.pop(minutes_id, None)
                self._dictionary_stages.pop(minutes_id, None)

        task.add_done_callback(clear_finished_task)

    async def _set_dictionary_stage(self, minutes_id: str, stage: str) -> None:
        if minutes_id not in self._dictionary_stages:
            return
        self._dictionary_stages[minutes_id] = stage
        await self._broadcast({
            "type": "dictionary_update_stage",
            "data": {"minutes_id": minutes_id, "stage": stage, "stage_label": DICTIONARY_STAGE_LABELS[stage]},
        })

    async def _run_durable_phase_b(
        self,
        minutes_id: str,
        provider_name: str | None,
    ) -> None:
        started = time.monotonic()
        await self._broadcast({
            "type": "dictionary_update_started",
            "data": {
                "minutes_id": minutes_id,
                "stage_label": DICTIONARY_STAGE_LABELS["dictionary_waiting"],
            },
        })
        discard_draft = True
        try:
            await self._run_phase_b(minutes_id, provider_name)
        except asyncio.CancelledError:
            # Shutdown cancellation keeps the durable entry for the next launch.
            logger.info("[phase-b] cancelled %s elapsed=%.1fs", minutes_id, time.monotonic() - started)
            raise
        except (TimeoutError, SummaryError) as exc:
            code = exc.code.value if isinstance(exc, SummaryError) else SummaryErrorCode.TIMEOUT.value
            logger.warning("[phase-b] failed %s code=%s elapsed=%.1fs", minutes_id, code, time.monotonic() - started)
        except Exception:
            discard_draft = False
            logger.exception("[phase-b] unexpected failure (%s)", minutes_id)
        self._durable_phase_b.pop(minutes_id, None)
        self._dictionary_stages.pop(minutes_id, None)
        self._persist_pending_jobs()
        if discard_draft:
            try:
                (DRAFTS_DIR / f"{minutes_id}.md").unlink(missing_ok=True)
            except Exception:
                pass
        logger.info("[phase-b] finished %s elapsed=%.1fs", minutes_id, time.monotonic() - started)
        await self._broadcast({
            "type": "dictionary_update_finished",
            "data": {"minutes_id": minutes_id},
        })

    async def _run_phase_b(self, minutes_id: str, provider_name: str | None = None) -> None:
        """辞書整理 (Phase B) を fire-and-forget で実行する薄いラッパー。"""
        ai_cfg = config.get("minutes_ai") or {}
        flag = ai_cfg.get("auto_dictionary_update")
        if flag is None:
            flag = ai_cfg.get("auto_correct_dictionary", True)
        if not bool(flag):
            return

        provider_name = str(provider_name or ai_cfg.get("provider") or "ollama")
        local_model_lease = provider_name in {"ollama", "claude_code", "codex"}
        queued_at = time.monotonic()
        if local_model_lease:
            from src.audio.resource_monitor import model_resource_gate

            await model_resource_gate.acquire_llm_async()
        try:
            logger.info(
                "[phase-b] start %s provider=%s resource_wait=%.1fs timeout=%ss",
                minutes_id, provider_name, time.monotonic() - queued_at, DICTIONARY_UPDATE_TIMEOUT,
            )
            await self._apply_dictionary_corrections(minutes_id, provider_name, ai_cfg)
        finally:
            if local_model_lease:
                from src.audio.resource_monitor import model_resource_gate

                model_resource_gate.release_llm()

    async def _apply_dictionary_corrections(self, minutes_id: str, provider_name: str, ai_cfg: dict) -> int:
        """Phase B: 要約完了直後に呼ばれる辞書整理。

        - transcript + docs + 既存辞書を LLM に渡し、new_glossary と new_corrections を取得
        - new_corrections は transcript に適用して DB を更新
        - 新規 glossary 用語と新規 corrections ペアを project に追記

        Returns:
            適用された置換の出現回数の合計 (0 = 何もしなかった)
        """
        from src.storage.db import db
        from src.project.manager import project_manager
        from src.dictionary.corrector import update_dictionary_from_meeting
        from src.dictionary.replacer import apply_corrections

        record = db.get_minutes(minutes_id)
        if record is None or not record.get("project_id"):
            return 0
        project = project_manager.get(record["project_id"])
        if project is None:
            return 0

        seg_raw = record.get("transcript")
        if isinstance(seg_raw, str):
            try:
                seg_raw = json.loads(seg_raw)
            except Exception:
                return 0
        if not isinstance(seg_raw, list) or not seg_raw:
            return 0

        transcript_text = "\n".join(
            str(s.get("text", "")).strip() for s in seg_raw if s.get("text")
        )
        if not transcript_text.strip():
            return 0

        glossary_terms = list(project.glossary)
        existing_corrections = [
            {"wrong": c.wrong, "correct": c.correct}
            for c in (project.corrections or [])
        ]

        threshold = float(ai_cfg.get("correction_confidence", 0.85))

        await self._set_dictionary_stage(minutes_id, "dictionary_reading")
        started = time.monotonic()
        extraction_error = None
        try:
            result = await update_dictionary_from_meeting(
                transcript_text,
                existing_glossary=glossary_terms,
                existing_corrections=existing_corrections,
                project_name=project.name,
                doc_dirs=list(project.doc_dirs),
                repo_path=project.repo_path,
                provider_name=provider_name,
                ai_cfg=ai_cfg,
                timeout_sec=DICTIONARY_UPDATE_TIMEOUT,
                confidence_threshold=threshold,
                on_extracting=lambda: self._set_dictionary_stage(minutes_id, "dictionary_extracting"),
            )
        except (SummaryError, TimeoutError) as exc:
            # 保存済み辞書による補正は、新しい候補を取得できなかった場合も行う。
            extraction_error = exc
            result = {"new_glossary": [], "new_corrections": []}
        new_glossary = result["new_glossary"]
        new_pairs = result["new_corrections"]
        logger.info(
            "[phase-b] candidates %s elapsed=%.1fs terms=%d corrections=%d",
            minutes_id, time.monotonic() - started, len(new_glossary), len(new_pairs),
        )
        await self._set_dictionary_stage(minutes_id, "dictionary_saving")

        # LLM の待機中に再文字起こしや辞書編集が行われた場合は最新値を使う。
        latest_record = db.get_minutes(minutes_id)
        if latest_record is None:
            return 0
        latest_segments = latest_record.get("transcript")
        if isinstance(latest_segments, str):
            try:
                latest_segments = json.loads(latest_segments)
            except Exception:
                return 0
        if not isinstance(latest_segments, list):
            return 0
        latest_project = project_manager.get(record["project_id"])
        if latest_project is None:
            return 0
        current_corrections = [
            {"wrong": c.wrong, "correct": c.correct}
            for c in (latest_project.corrections or [])
        ]

        # ─── transcript への補正適用 ───
        all_pairs = current_corrections + [
            {"wrong": p["wrong"], "correct": p["correct"]} for p in new_pairs
        ]
        replaced_count = 0
        if all_pairs:
            replaced_segments, replaced_count = apply_corrections(latest_segments, all_pairs)
            if replaced_count > 0:
                try:
                    if not db.update_transcript(minutes_id, replaced_segments):
                        replaced_count = 0
                except Exception as e:
                    logger.warning("[phase-b] update_transcript failed: %s", e)
                    replaced_count = 0

        # ─── project への追記 (glossary + corrections) ───
        if new_pairs or new_glossary:
            try:
                from src.project.models import CorrectionPair, ProjectUpdate

                update_payload: dict = {}
                added_pair_count = 0
                added_term_count = 0

                if new_pairs:
                    known_pairs = {
                        (c.wrong, c.correct)
                        for c in (latest_project.corrections or [])
                    }
                    additions: list[CorrectionPair] = []
                    for p in new_pairs:
                        key = (p["wrong"], p["correct"])
                        if key in known_pairs:
                            continue
                        additions.append(CorrectionPair(wrong=p["wrong"], correct=p["correct"]))
                    if additions:
                        update_payload["corrections"] = list(latest_project.corrections or []) + additions
                        added_pair_count = len(additions)

                if new_glossary:
                    known_terms = set(latest_project.glossary or [])
                    additions_g: list[str] = []
                    for g in new_glossary:
                        term = g.get("term", "")
                        if term and term not in known_terms:
                            additions_g.append(term)
                            known_terms.add(term)
                    if additions_g:
                        update_payload["glossary"] = list(latest_project.glossary or []) + additions_g
                        added_term_count = len(additions_g)

                if update_payload:
                    project_manager.update(latest_project.id, ProjectUpdate(**update_payload))
                    logger.info(
                        "[phase-b] project %s updated: +%d glossary, +%d corrections",
                        project.id, added_term_count, added_pair_count,
                    )
            except Exception as e:
                logger.warning("[phase-b] project update failed: %s", e)

        if replaced_count > 0 or new_glossary or new_pairs:
            try:
                await self._broadcast({
                    "type": "transcript_corrected",
                    "data": {
                        "minutes_id": minutes_id,
                        "replaced_count": replaced_count,
                        "new_pairs": new_pairs,
                        "new_glossary": new_glossary,
                    },
                })
            except Exception:
                pass

        if replaced_count > 0:
            logger.info(
                "[phase-b] applied %d replacement(s) on minutes %s",
                replaced_count, minutes_id,
            )
        if extraction_error is not None:
            raise extraction_error
        return replaced_count

def build_project_context(
    project_id: str | None, project_manager: Any
) -> ProjectContext | None:
    if not project_id:
        return None
    try:
        project = project_manager.get(project_id)
    except Exception:
        return None
    if project is None:
        return None
    # project は Pydantic Model (Project)
    members_raw = getattr(project, "members", []) or []
    members: list[dict[str, str]] = []
    for m in members_raw:
        name = getattr(m, "name", "") if not isinstance(m, dict) else m.get("name", "")
        role = getattr(m, "role", "") if not isinstance(m, dict) else m.get("role", "")
        if name:
            members.append({"name": str(name), "role": str(role or "")})
    glossary = list(getattr(project, "glossary", []) or [])
    repo_path = getattr(project, "repo_path", None)
    doc_dirs = getattr(project, "doc_dirs", []) or []
    return ProjectContext(
        name=str(getattr(project, "name", "") or ""),
        members=members,
        glossary=[str(g) for g in glossary if g],
        repo_path=str(repo_path) if repo_path else None,
        doc_dirs=[str(d) for d in doc_dirs if d],
    )


# ─── 起動時リカバリ ────────────────────────────────────

def recover_drafts() -> int:
    """``~/.seam/summary_drafts/{id}.md`` を全件チェックして DB に書き戻す。

    DB の summary が空なら draft で埋め、既に値があれば辞書整理を復元する。

    Returns:
        リカバリ件数
    """
    from src.storage.db import db

    if not DRAFTS_DIR.exists():
        return 0

    recovered = 0
    for draft_path in DRAFTS_DIR.glob("*.md"):
        minutes_id = draft_path.stem
        try:
            record = db.get_minutes(minutes_id)
        except Exception as e:
            logger.warning("[recovery] DB read failed (%s): %s", minutes_id, e)
            continue

        if record is None:
            # 議事録自体が削除された後の draft → 破棄
            try:
                draft_path.unlink()
            except Exception:
                pass
            continue

        existing_summary = str(record.get("summary") or "").strip()
        runner = get_runner()
        llm_model = str(record.get("llm_model") or "")
        provider_name = (
            llm_model.split(":", 1)[0]
            if ":" in llm_model
            else runner.pending_provider(minutes_id)
        )
        if existing_summary:
            # 要約保存後に落ちた場合、このdraftを辞書整理の復元マーカーに使う。
            if runner.persist_phase_b_intent(minutes_id, provider_name):
                try:
                    draft_path.unlink()
                except Exception:
                    pass
            continue

        # summary 空 → draft で埋める
        try:
            text = draft_path.read_text(encoding="utf-8")
            if not db.update_summary(minutes_id, text, llm_model="recovered_draft"):
                logger.warning("[recovery] draft target disappeared: %s", minutes_id)
                continue
            if runner.persist_phase_b_intent(minutes_id, provider_name):
                draft_path.unlink()
            recovered += 1
            logger.info("[recovery] recovered summary draft: %s", minutes_id)
        except Exception as e:
            logger.warning("[recovery] failed (%s): %s", minutes_id, e)

    if recovered:
        logger.info("[recovery] recovered %d summary draft(s)", recovered)
    return recovered


# ─── module-level singleton ────────────────────────────

_runner: SummaryRunner | None = None


def get_runner() -> SummaryRunner:
    """グローバル singleton runner を取得。

    FastAPI startup hook で初期化 + start() を呼ぶ想定。
    """
    global _runner
    if _runner is None:
        _runner = SummaryRunner()
    return _runner


def set_broadcaster(broadcaster: WSBroadcaster) -> None:
    """singleton runner に broadcaster を後付けする。"""
    runner = get_runner()
    runner._broadcaster = broadcaster
