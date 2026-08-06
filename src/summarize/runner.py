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
    format_transcript_segments,
    is_too_short_for_summary,
)
from .registry import get_provider

logger = logging.getLogger(__name__)

DRAFTS_DIR = APP_DIR / "summary_drafts"

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
        }


@dataclass
class _Job:
    minutes_id: str
    provider_name: str | None     # None ならconfig既定
    enqueued_at: float
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
        self._current: _Job | None = None
        self._current_provider: SummarizerProvider | None = None
        self._worker_task: asyncio.Task | None = None
        self._broadcaster = broadcaster
        self._shutdown = False

    # ─── lifecycle ────────────────────────────────────

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        self._worker_task = asyncio.create_task(
            self._worker(), name="summary-runner"
        )
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

    # ─── public API ────────────────────────────────────

    def enqueue(
        self, minutes_id: str, *, provider_name: str | None = None
    ) -> None:
        """要約ジョブを投入する。

        同一minutes_idの既存ジョブがあれば cancel して新ジョブで上書き。
        """
        existing = self._jobs.get(minutes_id)
        if existing is not None:
            existing.cancel_event.set()
            self._jobs.pop(minutes_id, None)
            logger.info(
                "[summary] cancelling existing job for re-enqueue: %s", minutes_id
            )

        job = _Job(
            minutes_id=minutes_id,
            provider_name=provider_name,
            enqueued_at=time.time(),
        )
        self._jobs[minutes_id] = job
        self._statuses[minutes_id] = JobStatus(
            minutes_id=minutes_id,
            state="queued",
            provider=provider_name,
        )
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

    def get_status(self, minutes_id: str) -> JobStatus | None:
        return self._statuses.get(minutes_id)

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
        if job is None:
            return False
        job.cancel_event.set()
        # 進行中ジョブだったら provider 側もcancel
        if self._current is not None and self._current.minutes_id == minutes_id:
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
                self._jobs.pop(job.minutes_id, None)
                self._statuses[job.minutes_id] = JobStatus(
                    minutes_id=job.minutes_id, state="cancelled"
                )
                continue

            self._current = job
            local_model_lease = False
            try:
                provider_name = job.provider_name or str(
                    (config.get("minutes_ai") or {}).get("provider", "ollama")
                )
                if provider_name in {"ollama", "claude_code", "codex"}:
                    from src.audio.resource_monitor import model_resource_gate

                    await model_resource_gate.acquire_llm_async()
                    local_model_lease = True
                    # CA-09: LLM を起動する前に Whisper の MLX モデルを解放する。
                    try:
                        from src.transcribe.streaming import unload_model

                        unload_model()
                    except Exception as exc:
                        logger.warning("Whisper unload before local LLM failed: %s", exc)
                await self._process_job(job)
            except Exception as e:
                logger.exception("[summary] worker unhandled error: %s", e)
            finally:
                if local_model_lease:
                    from src.audio.resource_monitor import model_resource_gate

                    model_resource_gate.release_llm()
                self._current = None
                self._current_provider = None
                self._jobs.pop(job.minutes_id, None)

    async def _process_job(self, job: _Job) -> None:
        from src.storage.db import db
        from src.project.manager import project_manager
        from src.pipeline.state import Stage, STAGE_LABELS

        minutes_id = job.minutes_id
        status = self._statuses[minutes_id]
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
        if is_too_short_for_summary(transcript_text):
            logger.info("[summary] transcript too short, skipping: %s", minutes_id)
            status.state = "skipped"
            status.finished_at = time.time()
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
                await self._broadcast_failed(status)
                return

        # 5. provider取得
        try:
            provider = get_provider(provider_name, ai_cfg)
        except SummaryError as e:
            self._mark_failed(status, e.code.value, e.message)
            await self._broadcast_failed(status)
            return

        # 6. health check
        try:
            health = await provider.health_check()
            if not health.ok:
                self._mark_failed(status, health.code, health.message)
                await self._broadcast_failed(status)
                return
            status.model = health.model
        except Exception as e:
            logger.exception("[summary] health_check failed: %s", e)
            self._mark_failed(
                status, SummaryErrorCode.UNKNOWN.value, str(e)
            )
            await self._broadcast_failed(status)
            return

        # 7. ProjectContext 構築
        project = self._build_project_context(record.get("project_id"), project_manager)

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

        async def on_token(chunk: str) -> None:
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
            if not label:
                return
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
            result = await provider.generate(
                transcript_text,
                project=project,
                on_token=on_token,
                on_activity=on_activity,
                timeout_sec=timeout_sec,
            )
        except SummaryError as e:
            if e.code == SummaryErrorCode.CANCELLED or job.cancel_event.is_set():
                status.state = "cancelled"
                status.finished_at = time.time()
                await self._broadcast({
                    "type": "summary_cancelled",
                    "data": {"minutes_id": minutes_id},
                })
                return
            self._mark_failed(status, e.code.value, e.message)
            await self._broadcast_failed(status)
            return
        except asyncio.CancelledError:
            status.state = "cancelled"
            status.finished_at = time.time()
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
            await self._broadcast_failed(status)
            return

        # 9. draft保存 → DB更新 → draft削除
        await self._save_with_recovery(record, status, result)

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

    async def _broadcast_failed(self, status: JobStatus) -> None:
        await self._broadcast({
            "type": "summary_failed",
            "data": {
                "minutes_id": status.minutes_id,
                "error_code": status.error_code,
                "message": status.error_message,
            },
        })

    async def _save_with_recovery(
        self, record: dict, status: JobStatus, result: SummaryResult
    ) -> None:
        """要約結果を draft → DB → draft削除 の順で保存。

        DB更新失敗時、draft が残るのでアプリ起動時の recover_drafts でリカバリ可。
        """
        from src.storage.db import db
        from src.pipeline.state import Stage, STAGE_LABELS

        minutes_id = status.minutes_id
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

        DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        draft_path = DRAFTS_DIR / f"{minutes_id}.md"

        # 要約本体 (タイトルセクション無し、概要〜議論ハイライトのみ)
        summary_body = result.text
        try:
            draft_path.write_text(summary_body, encoding="utf-8")
        except Exception as e:
            logger.warning("[summary] failed to write draft (%s): %s", minutes_id, e)

        new_title: str | None = None
        try:
            db.update_summary(
                minutes_id,
                summary_body,
                llm_model=f"{result.provider}:{result.model}",
            )
        except Exception as e:
            logger.exception("[summary] DB update failed: %s", e)
            self._mark_failed(
                status, SummaryErrorCode.UNKNOWN.value, f"DB更新に失敗: {e}"
            )
            await self._broadcast_failed(status)
            return

        # DB更新成功 → draft削除
        try:
            if draft_path.exists():
                draft_path.unlink()
        except Exception:
            pass

        # ─── タイトル生成 (別リクエスト、失敗しても要約結果は保持) ───
        from src.config import config as _cfg

        ai_cfg = _cfg.get("minutes_ai") or {}
        title_enabled = bool(ai_cfg.get("generate_title", True))
        if title_enabled:
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

                    project_now = self._build_project_context(
                        record_now.get("project_id") if record_now else None,
                        __import__("src.project.manager", fromlist=["project_manager"]).project_manager,
                    )
                    raw_title = await provider_obj.generate_title(
                        transcript_for_title,
                        project=project_now,
                        timeout_sec=60,
                    )
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
        await self._broadcast({
            "type": "summary_done",
            "data": {
                "minutes_id": minutes_id,
                "provider": result.provider,
                "model": result.model,
                "duration_sec": result.duration_sec,
                "input_chars": result.input_chars,
                "output_chars": result.output_chars,
                # タイトル生成成功時のみ含む。フロントは値があれば minutes.title を上書き表示する。
                "new_title": new_title,
            },
        })
        if new_title:
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
        asyncio.create_task(self._run_phase_b(status.minutes_id))

    async def _run_phase_b(self, minutes_id: str) -> None:
        """辞書整理 (Phase B) を fire-and-forget で実行する薄いラッパー。"""
        try:
            await self._apply_dictionary_corrections(minutes_id)
        except Exception as e:
            logger.warning("[phase-b] correction step failed (%s): %s", minutes_id, e)

    async def _apply_dictionary_corrections(self, minutes_id: str) -> int:
        """Phase B: 要約完了直後に呼ばれる辞書整理。

        - transcript + docs + 既存辞書を LLM に渡し、new_glossary と new_corrections を取得
        - new_corrections は transcript に適用して DB を更新
        - 新規 glossary 用語と新規 corrections ペアを project に追記

        Returns:
            適用された置換の出現回数の合計 (0 = 何もしなかった)
        """
        from src.storage.db import db
        from src.project.manager import project_manager
        from src.config import config as _cfg
        from src.dictionary.corrector import update_dictionary_from_meeting
        from src.dictionary.replacer import apply_corrections

        ai_cfg = _cfg.get("minutes_ai") or {}
        # 新設定キー auto_dictionary_update を最優先、旧キー auto_correct_dictionary は後方互換
        flag = ai_cfg.get("auto_dictionary_update")
        if flag is None:
            flag = ai_cfg.get("auto_correct_dictionary", True)
        if not bool(flag):
            return 0

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

        provider_name = str(ai_cfg.get("provider") or "ollama")
        threshold = float(ai_cfg.get("correction_confidence", 0.85))

        result = await update_dictionary_from_meeting(
            transcript_text,
            existing_glossary=glossary_terms,
            existing_corrections=existing_corrections,
            project_name=project.name,
            doc_dirs=list(project.doc_dirs),
            repo_path=project.repo_path,
            provider_name=provider_name,
            ai_cfg=ai_cfg,
            timeout_sec=180,
            confidence_threshold=threshold,
        )
        new_glossary = result["new_glossary"]
        new_pairs = result["new_corrections"]

        # ─── transcript への補正適用 ───
        all_pairs = existing_corrections + [
            {"wrong": p["wrong"], "correct": p["correct"]} for p in new_pairs
        ]
        replaced_count = 0
        if all_pairs:
            replaced_segments, replaced_count = apply_corrections(seg_raw, all_pairs)
            if replaced_count > 0:
                try:
                    db.update_transcript(minutes_id, replaced_segments)
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
                    known_pairs = {(c.wrong, c.correct) for c in (project.corrections or [])}
                    additions: list[CorrectionPair] = []
                    for p in new_pairs:
                        key = (p["wrong"], p["correct"])
                        if key in known_pairs:
                            continue
                        additions.append(CorrectionPair(wrong=p["wrong"], correct=p["correct"]))
                    if additions:
                        update_payload["corrections"] = list(project.corrections or []) + additions
                        added_pair_count = len(additions)

                if new_glossary:
                    known_terms = set(project.glossary or [])
                    additions_g: list[str] = []
                    for g in new_glossary:
                        term = g.get("term", "")
                        if term and term not in known_terms:
                            additions_g.append(term)
                            known_terms.add(term)
                    if additions_g:
                        update_payload["glossary"] = list(project.glossary or []) + additions_g
                        added_term_count = len(additions_g)

                if update_payload:
                    project_manager.update(project.id, ProjectUpdate(**update_payload))
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
        return replaced_count

    def _build_project_context(
        self, project_id: str | None, project_manager: Any
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

    DB の summary が空なら draft で埋める、既に値あれば draft 破棄。

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
        if existing_summary:
            # 既に summary 入っている → draft破棄
            try:
                draft_path.unlink()
            except Exception:
                pass
            continue

        # summary 空 → draft で埋める
        try:
            text = draft_path.read_text(encoding="utf-8")
            db.update_summary(minutes_id, text, llm_model="recovered_draft")
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
