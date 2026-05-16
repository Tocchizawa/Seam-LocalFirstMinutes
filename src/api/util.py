"""フロント (Tauri webview) から呼ぶ OS 操作ユーティリティ。

Tauri plugin (opener / clipboard-manager) は scope 設定や webview 制約で
うまく動かないケースがあるため、確実に動くバックエンド経由ルートを提供する。
macOS 専用の ``open`` / ``pbcopy`` を subprocess で呼ぶ。
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from src.api.errors import bad_request, forbidden, not_found
from src.config import config
from src.security import (
    is_allowed_request_origin,
    normalize_origin_list,
    resolve_existing_absolute_path,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/util", tags=["util"])


def _enforce_local_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    server_cfg = config.get("server") or {}
    extra_origins = normalize_origin_list(
        server_cfg.get("allowed_origins", []) if isinstance(server_cfg, dict) else [],
    )
    if not is_allowed_request_origin(origin, allowed_origins=extra_origins):
        raise forbidden("ORIGIN_NOT_ALLOWED", "origin is not allowed")


def _resolve_target_path(raw_path: str) -> Path:
    try:
        return resolve_existing_absolute_path(raw_path)
    except FileNotFoundError:
        target = Path(raw_path).expanduser()
        raise not_found("PATH_NOT_FOUND", f"パスが見つかりません: {target}")
    except ValueError:
        raise bad_request("INVALID_PATH", "絶対パスを指定してください")
    except Exception as e:
        raise bad_request("INVALID_PATH", f"不正なパスです: {e}")


class ClipboardWriteRequest(BaseModel):
    text: str = Field(max_length=2_000_000)


@router.post("/clipboard")
async def write_clipboard(body: ClipboardWriteRequest, request: Request) -> dict:
    """テキストをクリップボードに書き込む (macOS の pbcopy)。"""
    _enforce_local_origin(request)
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/pbcopy",
        stdin=asyncio.subprocess.PIPE,
    )
    try:
        await proc.communicate(body.text.encode("utf-8"))
    except Exception as e:
        logger.error("pbcopy failed: %s", e)
        raise bad_request("CLIPBOARD_WRITE_FAILED", f"クリップボード書き込み失敗: {e}")
    if proc.returncode != 0:
        raise bad_request(
            "CLIPBOARD_WRITE_FAILED",
            f"pbcopy exited with code {proc.returncode}",
        )
    return {"status": "ok", "bytes": len(body.text.encode("utf-8"))}


class OpenPathRequest(BaseModel):
    path: str


@router.post("/open")
async def open_path(body: OpenPathRequest, request: Request) -> dict:
    """パスを OS のデフォルトアプリで開く (macOS の open)。"""
    _enforce_local_origin(request)
    target = _resolve_target_path(body.path)
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/open",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
    except Exception as e:
        logger.error("open subprocess failed: %s", e)
        raise bad_request("OPEN_FAILED", f"開けませんでした: {e}")
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip() or f"exit {proc.returncode}"
        raise bad_request("OPEN_FAILED", f"open exited: {msg}")
    return {"status": "ok", "path": str(target), "exists": True}


class RevealPathRequest(BaseModel):
    path: str


@router.post("/reveal")
async def reveal_path(body: RevealPathRequest, request: Request) -> dict:
    """Finder でパスを選択状態で開く (macOS の open -R)。"""
    _enforce_local_origin(request)
    target = _resolve_target_path(body.path)
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/open",
            "-R",
            str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
    except Exception as e:
        logger.error("reveal failed: %s", e)
        raise bad_request("REVEAL_FAILED", f"Finder で開けませんでした: {e}")
    if proc.returncode != 0:
        msg = stderr.decode("utf-8", errors="replace").strip() or f"exit {proc.returncode}"
        raise bad_request("REVEAL_FAILED", f"open -R exited: {msg}")
    return {"status": "ok", "path": str(target)}


# 使用しない (logger を import しても出力先がなければ無音) と warning が出るのを避ける
_ = os
