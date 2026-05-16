"""Ollama サーバの起動状態を管理する。

責務:
  - `ollama` binary の発見 (Mac .app からは PATH が痩せるので補完)
  - `ollama serve` の存在確認 + 自動起動 (subprocess 経由)
  - ready になるまで poll
  - shutdown 時には終了させない (ユーザーが直接起動しているケースを尊重)

設定:
  - `ollama.auto_start` (top-level) — true なら自動起動を試みる
  - `minutes_ai.ollama.base_url` — 接続先
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434"

# 起動完了 (api/tags が 200 を返す) を待つ最長時間
DEFAULT_START_TIMEOUT_SEC = 30.0

# 自動起動を1回だけ試みるためのフラグ (重複起動防止)
_started_lock = asyncio.Lock()
_already_attempted = False


def _resolve_ollama_binary() -> str | None:
    """ollama バイナリのフルパスを返す。見つからなければ None。

    .app から spawn された場合 shell の PATH を継承しないので、
    homebrew 等の典型パスを明示的に確認する。
    """
    if shutil.which("ollama"):
        return shutil.which("ollama")
    candidates = [
        "/opt/homebrew/bin/ollama",
        "/usr/local/bin/ollama",
        os.path.expanduser("~/.local/bin/ollama"),
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


async def is_ollama_reachable(
    base_url: str = DEFAULT_BASE_URL, timeout_sec: float = 1.5,
) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


def _spawn_ollama_serve(binary: str) -> subprocess.Popen | None:
    """ollama serve を detach 起動。stdout/stderr は破棄。

    親プロセス終了後も生き続けるよう start_new_session=True。
    エラーを raise せずログだけ出す (best-effort)。
    """
    try:
        # OLLAMA_HOST が外部設定されている場合は尊重
        env = os.environ.copy()
        proc = subprocess.Popen(
            [binary, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        logger.info("spawned `ollama serve` (pid=%s, binary=%s)", proc.pid, binary)
        return proc
    except Exception as e:
        logger.warning("failed to spawn `ollama serve`: %s", e)
        return None


async def ensure_ollama_running(
    *,
    base_url: str = DEFAULT_BASE_URL,
    auto_start: bool = True,
    timeout_sec: float = DEFAULT_START_TIMEOUT_SEC,
) -> bool:
    """Ollama サーバが ready になっていることを保証する。

    Returns:
        True: ready (既に起動中 or この呼出しで起動成功)
        False: 起動できなかった (binary 未存在、auto_start=False、ready 待ちタイムアウト)
    """
    if await is_ollama_reachable(base_url):
        return True
    if not auto_start:
        return False

    global _already_attempted
    async with _started_lock:
        # 二重起動を避けるため再チェック
        if await is_ollama_reachable(base_url):
            return True

        binary = _resolve_ollama_binary()
        if not binary:
            logger.warning("ollama binary not found; cannot auto-start")
            return False

        proc = _spawn_ollama_serve(binary)
        if proc is None:
            return False
        _already_attempted = True

        # ready を poll で待つ
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if await is_ollama_reachable(base_url):
                return True
            # まだ立ち上がっていない。proc が即死していたら諦める。
            if proc.poll() is not None:
                logger.warning(
                    "ollama serve exited prematurely (rc=%s)", proc.returncode,
                )
                return False
            await asyncio.sleep(0.5)
        logger.warning(
            "ollama serve did not become ready within %.1fs", timeout_sec,
        )
        return False
