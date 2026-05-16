"""辞書 (用語集自動生成 / 誤転写補正) API。

CLI provider (claude_code/codex) の agentic mode は 1〜数分かかるので、
WebView の fetch がタイムアウトしないようジョブキュー方式にしてある。
- POST /auto-generate         → job を作成して即座に job_id を返す
- GET  /auto-generate/{job_id} → 進捗 / 完了結果を返す
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from src.api.errors import bad_request, not_found
from src.config import config
from src.dictionary.extractor import extract_glossary_from_docs
from src.project.manager import project_manager
from src.summarize.base import SummaryError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["dictionary"])


class GlossarySuggestion(BaseModel):
    term: str
    description: str = ""


# ─── in-memory job store ────────────────────────────────────────────
# 単一プロセス前提。再起動で揮発する (永続化不要)。

CLI_PROVIDERS = {"claude_code", "codex"}


class _GlossaryJob:
    __slots__ = (
        "id", "project_id", "state", "suggestions",
        "provider", "model", "is_cli", "current_activity", "activity_log",
        "error", "started_at", "finished_at",
    )

    def __init__(self, project_id: str) -> None:
        self.id: str = uuid.uuid4().hex[:12]
        self.project_id: str = project_id
        self.state: str = "running"  # running | done | error
        self.suggestions: list[dict] | None = None
        self.provider: str = ""
        self.model: str = ""
        self.is_cli: bool = False
        self.current_activity: str = ""
        # 最新の 20 件だけ保持 (UI 側で展開表示するかも)
        self.activity_log: list[str] = []
        self.error: dict | None = None  # {"code": str, "message": str}
        self.started_at: float = time.monotonic()
        self.finished_at: float | None = None

    def elapsed_sec(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return round(end - self.started_at, 2)

    def push_activity(self, msg: str) -> None:
        msg = (msg or "").strip()
        if not msg:
            return
        self.current_activity = msg
        self.activity_log.append(msg)
        if len(self.activity_log) > 20:
            self.activity_log = self.activity_log[-20:]


_jobs: dict[str, _GlossaryJob] = {}
_JOB_TTL_SEC = 1800  # 30分経過した完了ジョブは破棄


def _gc_jobs() -> None:
    now = time.monotonic()
    expired = [
        jid for jid, j in _jobs.items()
        if j.finished_at is not None and (now - j.finished_at) > _JOB_TTL_SEC
    ]
    for jid in expired:
        _jobs.pop(jid, None)


async def _run_glossary_job(job: _GlossaryJob, project_id: str) -> None:
    """バックグラウンドで extract_glossary_from_docs を呼び結果を job に格納。"""
    try:
        project = project_manager.get(project_id)
        if project is None:
            job.state = "error"
            job.error = {
                "code": "PROJECT_NOT_FOUND",
                "message": f"project '{project_id}' が見つかりません",
            }
            return

        ai_cfg = config.get("minutes_ai") or {}
        provider_name = str(ai_cfg.get("provider") or "ollama")
        job.provider = provider_name
        job.model = str((ai_cfg.get(provider_name) or {}).get("model", ""))
        job.is_cli = provider_name in CLI_PROVIDERS

        items = await extract_glossary_from_docs(
            project.name,
            list(project.doc_dirs),
            project.repo_path,
            provider_name=provider_name,
            ai_cfg=ai_cfg,
            existing_glossary=list(project.glossary),
            on_activity=job.push_activity,
        )
        job.suggestions = items
        job.state = "done"
    except SummaryError as e:
        job.state = "error"
        job.error = {"code": e.code.value, "message": e.message}
    except Exception as e:
        logger.exception("[glossary-job] unexpected error: %s", e)
        job.state = "error"
        job.error = {"code": "UNKNOWN", "message": str(e)}
    finally:
        job.finished_at = time.monotonic()


@router.post("/{project_id}/glossary/auto-generate")
async def start_auto_generate_glossary(project_id: str) -> dict:
    """非同期ジョブを起動し job_id を即返す。fetch のタイムアウト回避のため。"""
    project = project_manager.get(project_id)
    if project is None:
        raise not_found("PROJECT_NOT_FOUND", f"project '{project_id}' が見つかりません")
    if not project.doc_dirs and not project.repo_path:
        raise bad_request(
            "NO_DOCS",
            "doc_dirs または repo_path が設定されていません。",
        )

    _gc_jobs()
    job = _GlossaryJob(project_id)
    _jobs[job.id] = job
    asyncio.create_task(_run_glossary_job(job, project_id))
    return {"job_id": job.id, "state": job.state}


@router.get("/{project_id}/glossary/auto-generate/{job_id}")
async def get_auto_generate_status(project_id: str, job_id: str) -> dict:
    """job_id の進捗 / 完了結果を返す。"""
    job = _jobs.get(job_id)
    if job is None or job.project_id != project_id:
        raise not_found("JOB_NOT_FOUND", f"glossary job '{job_id}' が見つかりません")
    payload: dict = {
        "job_id": job.id,
        "state": job.state,
        "elapsed_sec": job.elapsed_sec(),
        "provider": job.provider,
        "model": job.model,
        "is_cli": job.is_cli,
        "current_activity": job.current_activity,
    }
    if job.state == "done":
        payload["suggestions"] = job.suggestions or []
    elif job.state == "error":
        payload["error"] = job.error or {"code": "UNKNOWN", "message": ""}
    return payload
