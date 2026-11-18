from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from src.api.errors import bad_request, forbidden
from src.config import config
from src.logging_config import setup_logging
from src.security import is_allowed_request_origin, normalize_origin_list

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


MAX_CUSTOM_SYSTEM_PROMPT_CHARS = 32_000


def _validate_launcher_settings(data: dict[str, Any]) -> None:
    minutes_ai = data.get("minutes_ai")
    if not isinstance(minutes_ai, dict):
        return
    prompt = minutes_ai.get("custom_system_prompt")
    if prompt is not None:
        if not isinstance(prompt, str) or len(prompt) > MAX_CUSTOM_SYSTEM_PROMPT_CHARS:
            raise bad_request(
                "INVALID_SYSTEM_PROMPT",
                f"custom_system_prompt は {MAX_CUSTOM_SYSTEM_PROMPT_CHARS} 文字以内の文字列にしてください",
            )
    for provider in ("claude_code", "codex"):
        provider_cfg = minutes_ai.get(provider)
        if not isinstance(provider_cfg, dict):
            continue
        launcher = provider_cfg.get("launcher_command")
        if launcher is not None:
            value = str(launcher)
            if any(ch in value for ch in ("\n", "\r", "\x00")) or len(value) > 320:
                raise bad_request(
                    "INVALID_LAUNCHER_COMMAND",
                    f"{provider}.launcher_command が不正です",
                )
        shell = provider_cfg.get("launcher_shell")
        if shell is not None:
            value = str(shell).strip()
            if not value or any(ch in value for ch in ("\n", "\r", "\x00")):
                raise bad_request(
                    "INVALID_LAUNCHER_SHELL",
                    f"{provider}.launcher_shell が不正です",
                )


@router.get("")
async def get_settings() -> dict[str, Any]:
    return config.data


@router.put("")
async def update_settings(data: dict[str, Any], request: Request) -> dict[str, Any]:
    origin = request.headers.get("origin")
    server_cfg = config.get("server") or {}
    extra_origins = normalize_origin_list(
        server_cfg.get("allowed_origins", []) if isinstance(server_cfg, dict) else [],
    )
    if not is_allowed_request_origin(origin, allowed_origins=extra_origins):
        raise forbidden("ORIGIN_NOT_ALLOWED", "origin is not allowed")

    _validate_launcher_settings(data)
    config.update(data)
    log_cfg = config.get("logging") or {}
    level = str(log_cfg.get("level", "INFO")).upper()
    if config.get("debug", "enabled", default=False):
        level = "DEBUG"
    setup_logging(
        level=level,
        log_dir=log_cfg.get("dir", "~/.seam/logs/"),
        max_size_mb=log_cfg.get("max_size_mb", 50),
        backup_count=log_cfg.get("backup_count", 3),
    )
    return config.data


@router.get("/summary/default-prompt")
async def get_default_summary_prompt() -> dict[str, str]:
    """要約の既定 system prompt を返す (UI の「デフォルトに戻す」用)。"""
    from src.summarize.prompts import SYSTEM_PROMPT

    return {"prompt": SYSTEM_PROMPT}


@router.get("/cli/codex/models")
async def get_codex_models() -> list[dict]:
    """Codex CLI が ~/.codex/models_cache.json にキャッシュしているモデル一覧を返す。

    Codex を一度でも起動していれば最新モデルがキャッシュされている。
    存在しない / 読み込みに失敗した場合は空配列を返す (UI 側でフォールバック)。
    """
    path = Path("~/.codex/models_cache.json").expanduser()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[codex] models_cache read failed: %s", e)
        return []
    out: list[dict] = []
    for m in data.get("models", []) or []:
        if not isinstance(m, dict):
            continue
        slug = str(m.get("slug") or "").strip()
        if not slug:
            continue
        # API 経由で使えないモデルや非表示のものはフィルタ
        if m.get("supported_in_api") is False:
            continue
        if str(m.get("visibility") or "").lower() == "hide":
            continue
        out.append({
            "slug": slug,
            "display_name": str(m.get("display_name") or slug),
            "description": str(m.get("description") or ""),
        })
    return out


@router.get("/cli/claude_code/models")
async def get_claude_code_models() -> list[dict]:
    """Claude Code の安定 alias (latest を指す) と既知の具体モデル ID を返す。

    Claude Code には公式の「現在使えるモデル一覧」エンドポイントが無いため
    バックエンド側でハードコード。エイリアスは将来の最新版を自動で指す。
    """
    return [
        {"slug": "haiku", "display_name": "haiku (高速・低コスト)"},
        {"slug": "sonnet", "display_name": "sonnet (推奨)"},
        {"slug": "opus", "display_name": "opus (最高精度)"},
    ]
