"""Seam — OpenAI API provider ユニットテスト

ネットワーク I/O 無し、SDK の AsyncOpenAI を mock してロジック検証。
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
from src.summarize.openai_api import (
    OpenAIProvider,
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

with patch("keyring.get_password", return_value=None):
    ok("has_api_key=False when keyring empty", not has_api_key())

with patch("keyring.get_password", return_value="sk-fake"):
    ok("has_api_key=True when keyring has value", has_api_key())

with patch("keyring.set_password") as ms:
    set_api_key("sk-test")
    ok("set_api_key calls keyring.set_password",
       ms.called and ms.call_args[0][0] == "seam-app")

try:
    set_api_key("")
    ok("empty token rejected", False, "expected ValueError")
except ValueError:
    ok("empty token rejected", True)

with patch("keyring.delete_password") as md:
    delete_api_key()
    ok("delete_api_key calls keyring.delete_password", md.called)


# ─────────────────────────────────────────────────────────
section("[2] health_check — APIキー無し")
# ─────────────────────────────────────────────────────────


async def _no_key():
    with patch("keyring.get_password", return_value=None):
        import os
        os.environ.pop("OPENAI_API_KEY", None)
        p = OpenAIProvider({"model": "gpt-4o-mini"})
        return await p.health_check()


health = asyncio.run(_no_key())
ok("no key → ok=False", not health.ok)
ok("no key → AUTH_FAILED code", health.code == "AUTH_FAILED")


# ─────────────────────────────────────────────────────────
section("[3] health_check — 認証成功 (mock)")
# ─────────────────────────────────────────────────────────


async def _auth_ok():
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=MagicMock(id="x"))
    fake_client.close = AsyncMock()
    with patch("keyring.get_password", return_value="sk-fake"):
        with patch("openai.AsyncOpenAI", return_value=fake_client):
            p = OpenAIProvider({"model": "gpt-4o-mini"})
            return await p.health_check()


health = asyncio.run(_auth_ok())
ok("auth ok → ok=True", health.ok)
ok("auth ok → READY code", health.code == "READY")
ok("auth ok → model echoed", health.model == "gpt-4o-mini")


# ─────────────────────────────────────────────────────────
section("[4] health_check — AuthenticationError")
# ─────────────────────────────────────────────────────────


async def _auth_fail():
    from openai import AuthenticationError

    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_resp = MagicMock(status_code=401)
    fake_client.chat.completions.create = AsyncMock(
        side_effect=AuthenticationError(
            message="Invalid", response=fake_resp, body={}
        )
    )
    fake_client.close = AsyncMock()
    with patch("keyring.get_password", return_value="sk-bad"):
        with patch("openai.AsyncOpenAI", return_value=fake_client):
            p = OpenAIProvider({"model": "gpt-4o-mini"})
            return await p.health_check()


health = asyncio.run(_auth_fail())
ok("invalid key → ok=False", not health.ok)
ok("invalid key → AUTH_FAILED code", health.code == "AUTH_FAILED")


# ─────────────────────────────────────────────────────────
section("[5] health_check — 404 (model not found)")
# ─────────────────────────────────────────────────────────


async def _model_not_found():
    from openai import APIStatusError

    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_resp = MagicMock(status_code=404)
    fake_client.chat.completions.create = AsyncMock(
        side_effect=APIStatusError(
            message="model not found", response=fake_resp, body={}
        )
    )
    fake_client.close = AsyncMock()
    with patch("keyring.get_password", return_value="sk-fake"):
        with patch("openai.AsyncOpenAI", return_value=fake_client):
            p = OpenAIProvider({"model": "gpt-bogus-9000"})
            return await p.health_check()


health = asyncio.run(_model_not_found())
ok("404 → ok=False", not health.ok)
ok("404 → MODEL_UNAVAILABLE code", health.code == "MODEL_UNAVAILABLE")


# ─────────────────────────────────────────────────────────
section("[6] generate — streaming success")
# ─────────────────────────────────────────────────────────


async def _gen_ok():
    captured: list[str] = []

    async def fake_stream():
        # OpenAI streaming events: each has .choices[0].delta.content
        for txt in ["## 概要\n", "テスト要約\n", "## 決定事項\n", "- なし\n"]:
            ev = MagicMock()
            ev.choices = [MagicMock(delta=MagicMock(content=txt))]
            yield ev

    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_stream())
    fake_client.close = AsyncMock()

    def on_token(t):
        captured.append(t)

    with patch("keyring.get_password", return_value="sk-fake"):
        with patch("openai.AsyncOpenAI", return_value=fake_client):
            p = OpenAIProvider({"model": "gpt-4o-mini", "max_tokens": 4096})
            ctx = ProjectContext(name="テスト", members=[], glossary=[])
            result = await p.generate(
                "[00:00] (田中) テスト発言。", project=ctx,
                on_token=on_token, timeout_sec=30,
            )
    return result, captured


result, tokens = asyncio.run(_gen_ok())
ok("generate returns SummaryResult", result.text != "")
ok("provider name = openai", result.provider == "openai")
ok("model echoed", result.model == "gpt-4o-mini")
ok("output_chars > 0", result.output_chars > 0)
ok("on_token received chunks", len(tokens) >= 4)
ok("text contains markdown header", "## 概要" in result.text)


# ─────────────────────────────────────────────────────────
section("[7] generate — RateLimitError → SummaryError(RATE_LIMIT)")
# ─────────────────────────────────────────────────────────


async def _gen_rate_limit():
    from openai import RateLimitError

    fake_resp = MagicMock(status_code=429)
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=RateLimitError(
            message="rate limited", response=fake_resp, body={}
        )
    )
    fake_client.close = AsyncMock()
    with patch("keyring.get_password", return_value="sk-fake"):
        with patch("openai.AsyncOpenAI", return_value=fake_client):
            p = OpenAIProvider({"model": "gpt-4o-mini"})
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
section("[8] generate — APIConnectionError → OFFLINE")
# ─────────────────────────────────────────────────────────


async def _gen_offline():
    from openai import APIConnectionError

    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_req = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=APIConnectionError(request=fake_req)
    )
    fake_client.close = AsyncMock()
    with patch("keyring.get_password", return_value="sk-fake"):
        with patch("openai.AsyncOpenAI", return_value=fake_client):
            p = OpenAIProvider({"model": "gpt-4o-mini"})
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
section("[9] cancel during streaming")
# ─────────────────────────────────────────────────────────


async def _cancel_during_stream():
    captured: list[str] = []
    p = OpenAIProvider({"model": "gpt-4o-mini"})

    class FakeStream:
        """async iterable + close() method を持つ stream mock。"""

        def __init__(self):
            self._i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i == 3:
                # 3 chunks 流したら cancel を発火
                await p.cancel()
            if self._i >= 20:
                raise StopAsyncIteration
            ev = MagicMock()
            ev.choices = [MagicMock(delta=MagicMock(content=f"chunk-{self._i} "))]
            self._i += 1
            await asyncio.sleep(0.01)
            return ev

        async def close(self):
            pass

    stream = FakeStream()

    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=stream)
    fake_client.close = AsyncMock()

    with patch("keyring.get_password", return_value="sk-fake"):
        with patch("openai.AsyncOpenAI", return_value=fake_client):
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
