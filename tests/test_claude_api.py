"""Seam — Claude API provider ユニットテスト

ネットワーク I/O を持たず、SDK の AsyncAnthropic を mock してロジック検証。
keyring 操作はテスト中に副作用が出ないよう全面 patch。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.summarize.base import (
    ProjectContext,
    SummaryError,
    SummaryErrorCode,
)
from src.summarize.claude_api import (
    ClaudeApiProvider,
    delete_api_key,
    has_api_key,
    set_api_key,
)

PASS = 0
FAIL = 0


def ok(name: str, cond: bool, hint: str = "") -> None:
    global PASS, FAIL
    if cond:
        print(f"  PASS: {name}")
        PASS += 1
    else:
        print(f"  FAIL: {name}{(' — ' + hint) if hint else ''}")
        FAIL += 1


def section(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


# ─────────────────────────────────────────────────────────
section("[1] keyring 操作 (mock)")
# ─────────────────────────────────────────────────────────

with patch("keyring.get_password", return_value=None) as mg:
    ok("has_api_key=False when keyring empty", not has_api_key())

with patch("keyring.get_password", return_value="sk-ant-fake-token"):
    ok("has_api_key=True when keyring has value", has_api_key())

# set_api_key
with patch("keyring.set_password") as ms:
    set_api_key("sk-ant-test")
    ok("set_api_key calls keyring.set_password",
       ms.called and ms.call_args[0][0] == "seam-app")

try:
    set_api_key("")
    ok("empty token rejected", False, "expected ValueError")
except ValueError:
    ok("empty token rejected", True)

# delete_api_key
with patch("keyring.delete_password") as md:
    delete_api_key()
    ok("delete_api_key calls keyring.delete_password", md.called)


# ─────────────────────────────────────────────────────────
section("[2] health_check — APIキー無し")
# ─────────────────────────────────────────────────────────


async def _no_key():
    with patch("keyring.get_password", return_value=None):
        # 環境変数も無効化
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            p = ClaudeApiProvider({"model": "claude-sonnet-4-6"})
            return await p.health_check()


health = asyncio.run(_no_key())
ok("no key → ok=False", not health.ok)
ok("no key → AUTH_FAILED code", health.code == "AUTH_FAILED")


# ─────────────────────────────────────────────────────────
section("[3] health_check — 認証成功 (mock)")
# ─────────────────────────────────────────────────────────


async def _auth_ok():
    fake_client = MagicMock()
    # messages.create は async
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=MagicMock(id="msg_x"))
    fake_client.close = AsyncMock()

    with patch("keyring.get_password", return_value="sk-ant-fake"):
        with patch("anthropic.AsyncAnthropic", return_value=fake_client):
            p = ClaudeApiProvider({"model": "claude-sonnet-4-6"})
            return await p.health_check()


health = asyncio.run(_auth_ok())
ok("auth ok → ok=True", health.ok)
ok("auth ok → READY code", health.code == "READY")
ok("auth ok → model echoed", health.model == "claude-sonnet-4-6")


# ─────────────────────────────────────────────────────────
section("[4] health_check — AuthenticationError")
# ─────────────────────────────────────────────────────────


async def _auth_fail():
    from anthropic import AuthenticationError

    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    # AuthenticationError は (message, response, body) の3引数
    fake_resp = MagicMock(status_code=401)
    fake_client.messages.create = AsyncMock(
        side_effect=AuthenticationError(
            message="Invalid key", response=fake_resp, body={}
        )
    )
    fake_client.close = AsyncMock()
    with patch("keyring.get_password", return_value="sk-ant-bad"):
        with patch("anthropic.AsyncAnthropic", return_value=fake_client):
            p = ClaudeApiProvider({"model": "claude-sonnet-4-6"})
            return await p.health_check()


health = asyncio.run(_auth_fail())
ok("invalid key → ok=False", not health.ok)
ok("invalid key → AUTH_FAILED code", health.code == "AUTH_FAILED")


# ─────────────────────────────────────────────────────────
section("[5] generate — streaming success")
# ─────────────────────────────────────────────────────────


async def _gen_ok():
    """messages.stream の async context manager を mock。"""
    captured_tokens: list[str] = []

    # text_stream を返す async iterator
    async def fake_text_stream():
        for chunk in ["## 概要\n", "テスト要約\n", "## 決定事項\n", "- 何もなし\n"]:
            yield chunk

    fake_stream = MagicMock()
    fake_stream.text_stream = fake_text_stream()

    # async context manager (async with stream() as s)
    fake_stream_ctx = MagicMock()
    fake_stream_ctx.__aenter__ = AsyncMock(return_value=fake_stream)
    fake_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.stream = MagicMock(return_value=fake_stream_ctx)
    fake_client.close = AsyncMock()

    def on_token(t):
        captured_tokens.append(t)

    with patch("keyring.get_password", return_value="sk-ant-fake"):
        with patch("anthropic.AsyncAnthropic", return_value=fake_client):
            p = ClaudeApiProvider({
                "model": "claude-sonnet-4-6",
                "max_tokens": 4096,
                "use_prompt_caching": True,
            })
            ctx = ProjectContext(name="テスト", members=[], glossary=[])
            result = await p.generate(
                "[00:00] (田中) テスト発言。",
                project=ctx,
                on_token=on_token,
                timeout_sec=30,
            )
    return result, captured_tokens


result, tokens = asyncio.run(_gen_ok())
ok("generate returns SummaryResult", result.text != "")
ok("provider name = claude_api", result.provider == "claude_api")
ok("model echoed", result.model == "claude-sonnet-4-6")
ok("output_chars > 0", result.output_chars > 0)
ok("on_token received chunks", len(tokens) >= 4)
ok("text contains markdown header", "## 概要" in result.text)


# ─────────────────────────────────────────────────────────
section("[6] generate — RateLimitError → SummaryError(RATE_LIMIT)")
# ─────────────────────────────────────────────────────────


async def _gen_rate_limit():
    from anthropic import RateLimitError

    fake_resp = MagicMock(status_code=429)

    async def raise_rate_limit():
        raise RateLimitError(
            message="rate limited", response=fake_resp, body={}
        )

    # streaming context が __aenter__ で raise する
    fake_stream_ctx = MagicMock()
    fake_stream_ctx.__aenter__ = AsyncMock(
        side_effect=RateLimitError(message="rate", response=fake_resp, body={})
    )
    fake_stream_ctx.__aexit__ = AsyncMock(return_value=False)
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.stream = MagicMock(return_value=fake_stream_ctx)
    fake_client.close = AsyncMock()

    with patch("keyring.get_password", return_value="sk-ant-fake"):
        with patch("anthropic.AsyncAnthropic", return_value=fake_client):
            p = ClaudeApiProvider({"model": "claude-sonnet-4-6"})
            try:
                await p.generate("test", timeout_sec=10)
                return None
            except SummaryError as e:
                return e


err = asyncio.run(_gen_rate_limit())
ok("RateLimitError → SummaryError raised", isinstance(err, SummaryError))
if isinstance(err, SummaryError):
    ok("RateLimitError → RATE_LIMIT code", err.code == SummaryErrorCode.RATE_LIMIT)


# ─────────────────────────────────────────────────────────
section("[7] generate — APIConnectionError → OFFLINE")
# ─────────────────────────────────────────────────────────


async def _gen_offline():
    from anthropic import APIConnectionError

    fake_stream_ctx = MagicMock()
    # APIConnectionError は request 引数を要求するので minimal mock
    fake_req = MagicMock()
    fake_stream_ctx.__aenter__ = AsyncMock(
        side_effect=APIConnectionError(request=fake_req)
    )
    fake_stream_ctx.__aexit__ = AsyncMock(return_value=False)
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.stream = MagicMock(return_value=fake_stream_ctx)
    fake_client.close = AsyncMock()

    with patch("keyring.get_password", return_value="sk-ant-fake"):
        with patch("anthropic.AsyncAnthropic", return_value=fake_client):
            p = ClaudeApiProvider({"model": "claude-sonnet-4-6"})
            try:
                await p.generate("test", timeout_sec=10)
                return None
            except SummaryError as e:
                return e


err = asyncio.run(_gen_offline())
ok("APIConnectionError → SummaryError", isinstance(err, SummaryError))
if isinstance(err, SummaryError):
    ok("APIConnectionError → OFFLINE code", err.code == SummaryErrorCode.OFFLINE)


# ─────────────────────────────────────────────────────────
section("[8] cancel during streaming")
# ─────────────────────────────────────────────────────────


async def _cancel_during_stream():
    """ストリーム5チャンク目で cancel() を呼んで CANCELLED が raise されること。"""
    captured: list[str] = []

    p = ClaudeApiProvider({"model": "claude-sonnet-4-6"})

    async def fake_text_stream():
        for i in range(20):
            if i == 3:
                # 3 chunks 流したら cancel を発火
                await p.cancel()
            yield f"chunk-{i} "
            await asyncio.sleep(0.01)

    fake_stream = MagicMock()
    fake_stream.text_stream = fake_text_stream()
    fake_stream_ctx = MagicMock()
    fake_stream_ctx.__aenter__ = AsyncMock(return_value=fake_stream)
    fake_stream_ctx.__aexit__ = AsyncMock(return_value=False)
    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.stream = MagicMock(return_value=fake_stream_ctx)
    fake_client.close = AsyncMock()

    with patch("keyring.get_password", return_value="sk-ant-fake"):
        with patch("anthropic.AsyncAnthropic", return_value=fake_client):
            try:
                await p.generate("test", on_token=lambda t: captured.append(t), timeout_sec=30)
                return None
            except SummaryError as e:
                return e


err = asyncio.run(_cancel_during_stream())
ok("cancel raises SummaryError", isinstance(err, SummaryError))
if isinstance(err, SummaryError):
    ok("cancel → CANCELLED code", err.code == SummaryErrorCode.CANCELLED)


# ─── Summary ───
print()
print("=" * 60)
print(f"  結果: {PASS} passed, {FAIL} failed")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
