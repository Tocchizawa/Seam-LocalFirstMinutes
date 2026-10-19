from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import ValidationError

from .models import (
    LiveSettings,
    semantic_operation_plan_schema,
    resolve_live_provider,
    validation_issue_summary,
)
from .prompts import build_prompt
from .providers.base import LiveProviderError
from .repository import LiveNoteStore, NoteError

logger = logging.getLogger(__name__)


class LiveNoteCoordinator:
    def __init__(self, path: Path, broadcast=None, provider_factory=None):
        self.path = path
        self.broadcast = broadcast
        self.provider_factory = provider_factory
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="live-notes-db"
        )
        self.store = None
        self.tasks = {}
        self.cancellations = {}
        self.inflight = asyncio.Semaphore(1)
        self.closed = False
        self.journal_sizes = {}

    async def call(self, method, *args, **kwargs):
        def run():
            if self.store is None:
                self.store = LiveNoteStore(self.path)
            return getattr(self.store, method)(*args, **kwargs)

        return await asyncio.get_running_loop().run_in_executor(self.executor, run)

    async def notify(self, sid, kind="live_note_status", **extra):
        snap = await self.call("snapshot", sid)
        if self.broadcast:
            try:
                await asyncio.wait_for(
                    self.broadcast(
                        {
                            "type": kind,
                            "data": {
                                "session_id": sid,
                                "revision": snap["note_revision"],
                                "status": snap["status"],
                                "provider": snap["provider"],
                                "model": snap["model"],
                                "pending_segments": snap["pending_segments"],
                                "queue_age_sec": snap["queue_age_sec"],
                                "processing": snap["processing"],
                                "last_error": snap["last_error"],
                                "minutes_id": snap["minutes_id"],
                                **extra,
                            },
                        }
                    ),
                    timeout=2,
                )
            except Exception:
                logger.warning("live note notification failed (session=%s)", sid)
        return snap

    def spawn(self, sid):
        if self.closed:
            return
        if sid not in self.tasks or self.tasks[sid].done():
            task = asyncio.create_task(self._supervise(sid), name="live-note-" + sid)
            self.tasks[sid] = task
            task.add_done_callback(self._task_done)

    async def _supervise(self, sid):
        while not self.closed:
            try:
                await self._run(sid)
                return
            except asyncio.CancelledError:
                raise
            except NoteError as exc:
                if exc.status == 404:
                    return
                logger.warning(
                    "live note worker retry (session=%s, code=%s)", sid, exc.code
                )
            except Exception:
                logger.warning("live note worker retry (session=%s)", sid)
            await asyncio.sleep(2)

    @staticmethod
    def _has_consent(provider, settings):
        from src.config import config

        current = config.get("live_notes", "consent", default={}) or {}
        # An explicit current false revokes an earlier grant. Never OR old consent.
        return bool(
            current.get(provider, settings.get("consent", {}).get(provider, False))
        )

    @staticmethod
    def _task_done(task):
        if not task.cancelled() and task.exception():
            logger.error("live note worker failed: %s", type(task.exception()).__name__)

    async def start(self, sid, pid, live_settings, minutes_ai):
        settings = LiveSettings.model_validate(live_settings).model_dump()
        name, cfg = resolve_live_provider(settings, minutes_ai)
        snap = await self.call("create", sid, pid, name, cfg, settings)
        try:
            from src.project.manager import project_manager

            project = project_manager.get(pid)
            if project:
                await self.call(
                    "set_metadata",
                    sid,
                    {
                        "project_name": project.name,
                        "glossary": [
                            str(entry)[:200] for entry in project.glossary[:100]
                        ],
                    },
                )
        except Exception:
            logger.warning("live meeting metadata unavailable (session=%s)", sid)
        self.spawn(sid)
        return snap

    async def ingest(self, sid, segments):
        try:
            snap = await self.call("ingest", sid, segments)
        except NoteError as e:
            if e.status == 404:
                return None
            raise
        self.spawn(sid)
        return snap

    async def stopped(self, sid, minutes_id=None):
        session = await self.call("session", sid)
        await self.call(
            "state",
            sid,
            stopped=1,
            flush_requested=1,
            status="paused" if session["paused"] else "finalizing",
        )
        if minutes_id:
            await self.call("link", sid, minutes_id)
        self.spawn(sid)
        return await self.notify(sid)

    async def human(self, sid, action, body, target=None):
        snap = await self.call("human", sid, action, body, target)
        operations = await self.call("event_operations", sid, snap["note_revision"])
        await self.notify(
            sid,
            "live_note_patch",
            actor="human",
            revision=snap["note_revision"],
            operations=operations,
        )
        return snap

    async def control(self, sid, action):
        s = await self.call("session", sid)
        if action == "consolidate":
            settings = json.loads(s["live_config_json"])
            if s["provider"] not in ("codex", "claude_code"):
                raise NoteError("CLI_PROVIDER_REQUIRED", "次回の録音前にCodexまたはClaudeを選択してください")
            if not self._has_consent(s["provider"], settings):
                raise NoteError("CONSENT_REQUIRED", "設定画面で発話テキストの送信に同意してください", 403)
            batch = await self.call("manual_consolidation_batch", sid)
            if batch:
                self.spawn(sid)
                return await self.notify(sid, reason="手動整理をキューに追加しました", consolidation_batch_id=batch["id"])
            return await self.notify(sid, reason="同じrevisionの整理がすでに処理中です")
        if action == "pause":
            if sid in self.cancellations:
                self.cancellations[sid].set()
            await self.call("state", sid, paused=1, status="paused")
        else:
            if action not in ("resume", "retry", "flush"):
                raise NoteError("INVALID_CONTROL", "不明な制御操作です")
            # Consent can be granted later without changing the pinned provider/model.
            settings = json.loads(s["live_config_json"])
            if s["provider"] not in ("codex", "claude_code"):
                raise NoteError(
                    "CLI_PROVIDER_REQUIRED",
                    "次回の録音前にCodexまたはClaudeを選択してください",
                )
            if not self._has_consent(s["provider"], settings):
                raise NoteError(
                    "CONSENT_REQUIRED",
                    "設定画面で発話テキストの送信に同意してください",
                    403,
                )
            # Consent is checked live by _run as well; no credential copying needed.
            await self.call(
                "state",
                sid,
                paused=0,
                status="finalizing" if s["stopped"] else "recording",
                last_error=None,
                failures=0,
                next_attempt_at=0,
                flush_requested=int(action == "flush" or bool(s["stopped"])),
            )
            self.spawn(sid)
        return await self.notify(sid)

    async def restore(self, journal_root=None):
        sessions = await self.call("recover")
        for s in sessions:
            await self.call("recover_link", s["session_id"])
            sid = s["session_id"]
            if journal_root:
                path = Path(journal_root) / sid / "transcript.jsonl"
                if path.exists():
                    try:
                        segments = []
                        for i, line in enumerate(path.read_text().splitlines()):
                            try:
                                seg = json.loads(line)
                                seg.setdefault("segment_id", f"legacy:{i}")
                                segments.append(seg)
                            except (ValueError, TypeError):
                                continue
                        await self.ingest(sid, segments)
                    except Exception:
                        logger.warning(
                            "live note journal recovery failed (session=%s)", sid
                        )
            if s["status"] not in ("aborted", "completed"):
                self.spawn(sid)
        return sessions

    async def remove(self, sid):
        if sid in self.cancellations:
            self.cancellations[sid].set()
        await self.call("state", sid, status="aborted", stopped=1)
        task = self.tasks.pop(sid, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def reconcile(self, sid):
        path = self.path.parent / "sessions" / sid / "transcript.jsonl"

        def read():
            if not path.exists():
                return None
            size = path.stat().st_size
            if self.journal_sizes.get(sid) == size:
                return None
            result = []
            for i, line in enumerate(path.read_text().splitlines()):
                try:
                    seg = json.loads(line)
                    seg.setdefault("segment_id", f"legacy:{i}")
                    result.append(seg)
                except (ValueError, TypeError):
                    continue
            return size, result

        data = await asyncio.to_thread(read)
        if data:
            size, segments = data
            await self.call("ingest", sid, segments)
            self.journal_sizes[sid] = size

    async def close(self):
        self.closed = True
        for cancel in self.cancellations.values():
            cancel.set()
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self.store:
            await self.call("close")
        self.executor.shutdown(wait=True)

    async def _run(self, sid):
        provider = None
        while not self.closed:
            try:
                await self.reconcile(sid)
                s = await self.call("session", sid)
            except NoteError as exc:
                if exc.status == 404:
                    return
                raise
            except Exception:
                logger.warning("live note durable ingest retry (session=%s)", sid)
                await asyncio.sleep(2)
                continue
            settings = json.loads(s["live_config_json"])
            snap = await self.call("snapshot", sid)
            if s["status"] == "aborted":
                return
            scheduled_consolidation = False
            if not snap["pending_segments"]:
                if s["stopped"]:
                    if s.get("finalize_pass_done"):
                        await self.call("state", sid, status="completed", flush_requested=0, paused=0)
                        await self.notify(sid, "live_note_completed")
                        return
                    # A stopped meeting gets one final editor pass even when the transcript cursor is caught up.
                    final_batch = await self.call("finalize_batch", sid)
                    if not final_batch:
                        await asyncio.sleep(1)
                        continue
                if s["flush_requested"]:
                    await self.call("state", sid, flush_requested=0)
                if not s["stopped"]:
                    consolidation = await self.call("consolidation_batch", sid)
                    if not consolidation:
                        await asyncio.sleep(1)
                        continue
                    scheduled_consolidation = True
            if (
                s["paused"]
                or s["failures"] >= 3
                or s["status"] == "error"
                and not s["next_attempt_at"]
            ):
                await asyncio.sleep(1)
                continue
            if time.time() < s["next_attempt_at"]:
                await asyncio.sleep(1)
                continue
            last = await self.call("last_call_time", sid)
            pending_chars = await self.call("pending_chars", sid)
            elapsed = time.time() - last if last else snap["queue_age_sec"]
            ready = (
                scheduled_consolidation
                or
                (elapsed >= settings["target_interval_sec"])
                or snap["queue_age_sec"] >= settings["max_wait_sec"]
                or pending_chars >= settings["min_pending_chars"]
                or s["flush_requested"]
                or s["stopped"] and not s.get("finalize_pass_done")
            )
            if not ready or last and time.time() - last < settings["min_interval_sec"]:
                await asyncio.sleep(1)
                continue
            if not s["stopped"]:
                from src.audio.resource_monitor import ResourceMonitor

                if ResourceMonitor().should_throttle(
                    cpu_threshold=90, memory_threshold=90
                ):
                    await self.notify(
                        sid, reason="録音を優先してAI整理を待機しています"
                    )
                    await asyncio.sleep(5)
                    continue
            # All recordings share one bounded external inference slot. Never hold a Whisper lease.
            async with self.inflight:
                s = await self.call("session", sid)
                if s["paused"]:
                    continue
                batch = await self.call("batch", sid)
                if not batch:
                    continue
                cancel = asyncio.Event()
                self.cancellations[sid] = cancel
                try:
                    if not self._has_consent(s["provider"], settings):
                        raise NoteError(
                            "CONSENT_REQUIRED",
                            "発話のクラウド送信への同意が必要です",
                            403,
                        )
                    if provider is None:
                        from .providers import create_provider

                        factory = self.provider_factory or create_provider
                        if s["provider"] not in ("codex", "claude_code"):
                            raise NoteError(
                                "CLI_PROVIDER_REQUIRED",
                                "ライブノートにはCodexまたはClaude CLIを選択してください",
                            )
                        provider = factory(
                            s["provider"], json.loads(s["provider_config_json"])
                        )
                        check = await provider.preflight()
                        if not check["ok"]:
                            provider = None
                            raise NoteError(
                                "CLI_UNAVAILABLE",
                                check.get("error", "CLIに必要な制限機能がありません"),
                            )
                    context = await self.call("context", sid, batch)
                    context_expansion_used = False
                    for repair in range(2):
                        if repair:
                            try:
                                await asyncio.wait_for(
                                    cancel.wait(), timeout=settings["min_interval_sec"]
                                )
                                raise asyncio.CancelledError()
                            except TimeoutError:
                                pass
                        if not self._has_consent(s["provider"], settings):
                            raise NoteError(
                                "CONSENT_REQUIRED",
                                "発話のクラウド送信への同意が取り消されました",
                                403,
                            )
                        cid = await self.call(
                            "start_call", sid, batch, context, repair=bool(repair and not context_expansion_used)
                        )
                        started = time.monotonic()
                        try:
                            await self.notify(sid)
                            if not self._has_consent(s["provider"], settings):
                                raise asyncio.CancelledError()
                            raw = await provider.infer_operations(
                                build_prompt(context),
                                semantic_operation_plan_schema(version=3),
                                timeout=settings["invocation_timeout_sec"],
                                cancellation=cancel,
                            )
                            await self.call(
                                "finish_call", cid, (time.monotonic() - started) * 1000
                            )
                            if cancel.is_set() or not self._has_consent(
                                s["provider"], settings
                            ):
                                raise asyncio.CancelledError()
                            if isinstance(raw, dict) and raw.get("context_request"):
                                if repair or context_expansion_used:
                                    raise NoteError("INVALID_CONTEXT_REQUEST", "文脈拡張は1回までです")
                                context_expansion_used = True
                                context = await self.call("expand_context", sid, context, raw["context_request"])
                                continue
                            result, conflicts = await self.call(
                                "apply", sid, batch, context, raw
                            )
                            await self.notify(
                                sid,
                                "live_note_patch",
                                actor="ai",
                                batch_id=batch["id"],
                                revision=result["note_revision"],
                                operations=await self.call(
                                    "event_operations", sid, result["note_revision"]
                                ),
                            )
                            if conflicts:
                                await self.notify(
                                    sid, "live_note_conflict", suggestions=conflicts
                                )
                            break
                        except (ValueError, NoteError, LiveProviderError) as exc:
                            if isinstance(exc, LiveProviderError) and not getattr(
                                exc, "structural", False
                            ):
                                await self.call(
                                    "finish_call",
                                    cid,
                                    (time.monotonic() - started) * 1000,
                                    "provider",
                                )
                                raise
                            await self.call(
                                "finish_call",
                                cid,
                                (time.monotonic() - started) * 1000,
                                "validation",
                            )
                            if (
                                context_expansion_used
                                or
                                repair
                                or batch["repair_calls"]
                                >= settings["max_structural_repair_calls"]
                                or isinstance(exc, NoteError)
                                and exc.code == "CALL_LIMIT"
                            ):
                                raise
                            context = {
                                **context,
                                "repair_instruction": "前回の出力は検証不合格です。指定schema、ID、出典、所有者制約を確認してJSONのみ返してください。",
                                "validation_errors": validation_issue_summary(exc)
                                if isinstance(exc, ValidationError)
                                else getattr(exc, "code", "INVALID_JSON"),
                            }
                        except BaseException:
                            await self.call(
                                "finish_call",
                                cid,
                                (time.monotonic() - started) * 1000,
                                "provider",
                            )
                            raise
                    await self.call(
                        "state",
                        sid,
                        status="finalizing" if s["stopped"] else "recording",
                        last_error=None,
                        next_attempt_at=0,
                        failures=0,
                    )
                    await self.notify(sid)
                except asyncio.CancelledError:
                    await self.call(
                        "fail_batch", batch["id"], "中断しました。発話は保存済みです"
                    )
                    if self.closed or asyncio.current_task().cancelling():
                        raise
                except Exception as exc:
                    if cancel.is_set():
                        await self.call(
                            "fail_batch",
                            batch["id"],
                            "AI整理を一時停止しました。発話は保存済みです",
                        )
                        continue
                    # Never log provider output or transcript contents.
                    code = (
                        exc.code
                        if isinstance(exc, (NoteError, LiveProviderError))
                        else type(exc).__name__
                    )
                    message = (
                        str(exc)[:300]
                        if isinstance(exc, (NoteError, LiveProviderError))
                        else "CLI処理に失敗しました。認証・接続状態を確認して再試行してください"
                    )
                    if isinstance(exc, ValidationError):
                        code = "PLAN_SCHEMA_INVALID"
                        message = (
                            "LLMの操作JSONが形式制約に合いません（"
                            + validation_issue_summary(exc)
                            + "）"
                        )
                    await self.call("fail_batch", batch["id"], message)
                    failures = s["failures"] + 1
                    retryable = not isinstance(exc, NoteError) and getattr(
                        exc, "retryable", True
                    )
                    delay = (
                        20 * (2 ** (failures - 1)) if retryable and failures < 3 else 0
                    )
                    if isinstance(exc, NoteError) and exc.code == "CALL_LIMIT":
                        delay = max(
                            1, (await self.call("call_limit_reset")) - time.time()
                        )
                        failures = s["failures"]
                    await self.call(
                        "state",
                        sid,
                        status="error",
                        last_error=f"{code}: {message}",
                        failures=failures,
                        next_attempt_at=time.time() + delay if delay else 0,
                    )
                    await self.notify(sid, "live_note_error", code=code)
                finally:
                    self.cancellations.pop(sid, None)


_coordinator = None


def get_coordinator():
    global _coordinator
    if _coordinator is None or _coordinator.closed:
        from src.api.ws import ws_manager
        from src.storage.db import db

        _coordinator = LiveNoteCoordinator(db._path, ws_manager.broadcast)
    return _coordinator
