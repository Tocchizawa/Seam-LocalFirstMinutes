"""Seam — Claude Code / Codex CLI provider ユニットテスト

実 binary を呼ばないよう subprocess を mock。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.summarize.base import (
    ProjectContext,
    SummaryError,
    SummaryErrorCode,
)
from src.summarize.claude_code import ClaudeCodeProvider
from src.summarize.codex import CodexProvider, _strip_ansi

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


# ─── helpers: subprocess を mock ───

def _make_fake_proc(stdout_lines: list[bytes], stderr: bytes = b"", returncode: int = 0):
    """async iter で stdout_lines を返し、return_code を await できる proc mock。"""
    proc = MagicMock()
    proc.returncode = returncode

    class _Stdout:
        def __init__(self, lines):
            self._lines = list(lines)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._lines:
                raise StopAsyncIteration
            return self._lines.pop(0)

    proc.stdout = _Stdout(stdout_lines)
    stderr_reader = MagicMock()
    stderr_reader.read = AsyncMock(return_value=stderr)
    proc.stderr = stderr_reader
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    proc.stdin = stdin
    proc.wait = AsyncMock(return_value=returncode)
    proc.communicate = AsyncMock(return_value=(b"\n".join(stdout_lines), stderr))
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


# ─────────────────────────────────────────────────────────
section("[1] ClaudeCodeProvider — binary not found")
# ─────────────────────────────────────────────────────────


async def _no_binary():
    with patch("shutil.which", return_value=None):
        p = ClaudeCodeProvider({"binary_path": "claude"})
        return await p.health_check()


h = asyncio.run(_no_binary())
ok("missing binary → ok=False", not h.ok)
ok("missing binary → MODEL_UNAVAILABLE", h.code == "MODEL_UNAVAILABLE")


# ─────────────────────────────────────────────────────────
section("[2] ClaudeCodeProvider — health_check ok (mock subprocess)")
# ─────────────────────────────────────────────────────────


async def _health_ok():
    fake_proc = _make_fake_proc([b"2.1.1 (Claude Code)\n"], returncode=0)
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            p = ClaudeCodeProvider({"binary_path": "claude", "model": "haiku"})
            return await p.health_check()


h = asyncio.run(_health_ok())
ok("health ok=True", h.ok)
ok("READY code", h.code == "READY")
ok("model echoed (haiku)", h.model == "haiku")


# ─────────────────────────────────────────────────────────
section("[3] ClaudeCodeProvider — generate streaming (stream-json mock)")
# ─────────────────────────────────────────────────────────


def _stream_json_lines() -> list[bytes]:
    """sample-json events を bytes line iterator 形式に。"""
    events = [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "## 概要\n"},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "テスト要約\n"},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "## 決定事項\n- なし"},
            },
        },
        {"type": "result", "total_cost_usd": 0.001},
    ]
    return [(json.dumps(e) + "\n").encode("utf-8") for e in events]


async def _gen_ok():
    captured: list[str] = []
    fake_proc = _make_fake_proc(_stream_json_lines(), returncode=0)
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            p = ClaudeCodeProvider({"binary_path": "claude", "model": "sonnet"})
            ctx = ProjectContext(name="テスト")
            result = await p.generate(
                "[00:00] (田中) hi",
                project=ctx,
                on_token=lambda t: captured.append(t),
                timeout_sec=30,
            )
    return result, captured


result, tokens = asyncio.run(_gen_ok())
ok("generate succeeded", result.text != "")
ok("provider name", result.provider == "claude_code")
ok("model from system event", result.model == "claude-sonnet-4-6")
ok("on_token captured deltas", len(tokens) == 3)
ok("text contains markdown", "## 概要" in result.text and "## 決定事項" in result.text)


# ─────────────────────────────────────────────────────────
section("[4] ClaudeCodeProvider — non-zero exit → PROVIDER_DOWN")
# ─────────────────────────────────────────────────────────


async def _gen_exit_err():
    # text_delta なし、exit 1
    fake_proc = _make_fake_proc(
        [b'{"type":"system","subtype":"init"}\n'],
        stderr=b"Authentication failed",
        returncode=1,
    )
    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            p = ClaudeCodeProvider({"binary_path": "claude"})
            try:
                await p.generate("test", timeout_sec=10)
                return None
            except SummaryError as e:
                return e


err = asyncio.run(_gen_exit_err())
ok("non-zero exit → SummaryError", isinstance(err, SummaryError))
if isinstance(err, SummaryError):
    ok("non-zero exit → PROVIDER_DOWN", err.code == SummaryErrorCode.PROVIDER_DOWN)


# ─────────────────────────────────────────────────────────
section("[5] ClaudeCodeProvider — cancel during streaming")
# ─────────────────────────────────────────────────────────


async def _cancel_during():
    p = ClaudeCodeProvider({"binary_path": "claude"})

    class _Stdout:
        def __init__(self):
            self._i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i >= 50:
                raise StopAsyncIteration
            if self._i == 2:
                # 2チャンク後にcancel
                await p.cancel()
            ev = {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": f"d{self._i} "},
                },
            }
            self._i += 1
            await asyncio.sleep(0.01)
            return (json.dumps(ev) + "\n").encode()

    proc = MagicMock()
    proc.returncode = None
    proc.stdout = _Stdout()
    stderr_reader = MagicMock()
    stderr_reader.read = AsyncMock(return_value=b"")
    proc.stderr = stderr_reader
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    proc.stdin = stdin
    proc.wait = AsyncMock(return_value=-15)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    with patch("shutil.which", return_value="/usr/local/bin/claude"):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            try:
                await p.generate("test", timeout_sec=30)
                return None, proc
            except SummaryError as e:
                return e, proc


err, proc_mock = asyncio.run(_cancel_during())
ok("cancel raises SummaryError", isinstance(err, SummaryError))
if isinstance(err, SummaryError):
    ok("cancel → CANCELLED code", err.code == SummaryErrorCode.CANCELLED)
ok("subprocess.terminate called", proc_mock.terminate.called)


# ─────────────────────────────────────────────────────────
section("[6] CodexProvider — binary not found")
# ─────────────────────────────────────────────────────────


async def _codex_no_binary():
    with patch("shutil.which", return_value=None):
        p = CodexProvider({"binary_path": "codex"})
        return await p.health_check()


h = asyncio.run(_codex_no_binary())
ok("codex missing → ok=False", not h.ok)
ok("codex missing → MODEL_UNAVAILABLE", h.code == "MODEL_UNAVAILABLE")


# ─────────────────────────────────────────────────────────
section("[7] CodexProvider — health_check ok (mock)")
# ─────────────────────────────────────────────────────────


async def _codex_health_ok():
    fake_proc = _make_fake_proc([b"codex 0.50.0\n"], returncode=0)
    with patch("shutil.which", return_value="/opt/homebrew/bin/codex"):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            p = CodexProvider({"binary_path": "codex"})
            return await p.health_check()


h = asyncio.run(_codex_health_ok())
ok("codex health ok", h.ok)
ok("codex READY", h.code == "READY")


# ─────────────────────────────────────────────────────────
section("[8] CodexProvider — generate captures stdout")
# ─────────────────────────────────────────────────────────


async def _codex_gen():
    lines = [
        b"## \xe6\xa6\x82\xe8\xa6\x81\n",   # ## 概要 (UTF-8)
        b"summary text\n",
        b"## TODO\n",
        b"- nothing\n",
    ]
    fake_proc = _make_fake_proc(lines, returncode=0)
    captured: list[str] = []
    with patch("shutil.which", return_value="/opt/homebrew/bin/codex"):
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_proc),
        ):
            p = CodexProvider({"binary_path": "codex"})
            return await p.generate(
                "[00:00] hi", on_token=lambda t: captured.append(t), timeout_sec=10,
            ), captured


res, toks = asyncio.run(_codex_gen())
ok("codex generate text non-empty", res.text != "")
ok("codex provider name", res.provider == "codex")
ok("codex on_token chunks", len(toks) == 4)
ok("codex output preserves Japanese", "概要" in res.text)


# ─────────────────────────────────────────────────────────
section("[9] _strip_ansi: ANSI escape除去")
# ─────────────────────────────────────────────────────────

raw = "\x1b[1;31mError\x1b[0m: bad thing\n"
ok("ANSI removed", _strip_ansi(raw) == "Error: bad thing\n")
ok("plain pass-through", _strip_ansi("hello") == "hello")


# ─────────────────────────────────────────────────────────
section("[10] CodexProvider — unsupported model fallback to CLI default")
# ─────────────────────────────────────────────────────────


async def _codex_health_model_fallback():
    version_proc = _make_fake_proc([b"codex 0.50.0\n"], returncode=0)
    bad_model_proc = _make_fake_proc(
        [],
        stderr=(
            b'{"type":"invalid_request_error","message":"The model is not supported '
            b'when using Codex with a ChatGPT account."}'
        ),
        returncode=1,
    )
    fallback_ok_proc = _make_fake_proc([b"OK\n"], returncode=0)
    mock_exec = AsyncMock(side_effect=[version_proc, bad_model_proc, fallback_ok_proc])
    with patch("asyncio.create_subprocess_exec", new=mock_exec):
        p = CodexProvider({
            "binary_path": "codex",
            "model": "gpt-5",
        })
        health = await p.health_check()
    return health


h = asyncio.run(_codex_health_model_fallback())
ok("fallback health ok", h.ok)
ok("fallback model label", h.model == "codex-default")
ok("fallback message note", "fallback" in (h.message or ""))


# ─────────────────────────────────────────────────────────
section("[11] CodexProvider — launcher_command uses zsh -ic")
# ─────────────────────────────────────────────────────────


async def _codex_launcher_invocation():
    version_proc = _make_fake_proc([b"codex 0.50.0\n"], returncode=0)
    probe_proc = _make_fake_proc([b"OK\n"], returncode=0)
    mock_exec = AsyncMock(side_effect=[version_proc, probe_proc])
    with patch("asyncio.create_subprocess_exec", new=mock_exec):
        p = CodexProvider({
            "launcher_command": "source ~/.zshrc; my_codex",
            "launcher_shell": "/bin/zsh",
            "launcher_interactive": True,
            "model": "",
        })
        health = await p.health_check()
    first_call = mock_exec.await_args_list[0].args
    return health, first_call


h, argv = asyncio.run(_codex_launcher_invocation())
ok("launcher health ok", h.ok)
ok("launcher uses zsh", len(argv) >= 3 and argv[0] == "/bin/zsh")
ok("launcher uses -ic", len(argv) >= 3 and argv[1] == "-ic")
ok("launcher command contains --version", len(argv) >= 3 and "--version" in str(argv[2]))


# ─── Summary ───
print()
print("=" * 60)
print(f"  結果: {PASS} passed, {FAIL} failed")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
