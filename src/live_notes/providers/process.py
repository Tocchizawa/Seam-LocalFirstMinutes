"""Bounded CLI process supervision shared by the two live-only adapters."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from typing import Any

from .base import LiveProviderError

MAX_STDOUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 128 * 1024
MAX_INPUT_BYTES = 1024 * 1024


def cli_failure(detail: str, exit_code: int | None = None) -> LiveProviderError:
    """Map diagnostics to safe messages; never return raw output or meeting text."""
    low = detail.lower()
    field = re.search(r"unknown configuration field [`']([a-z0-9_.-]{1,80})[`']", low)
    if field:
        return LiveProviderError(
            f"CLIが起動設定「{field[1]}」に対応していません。SeamとCLIの互換性を確認してください",
            False,
            code="CLI_CONFIG_UNSUPPORTED",
        )
    for markers, code, message, retryable in (
        (
            (
                "not logged in",
                "not authenticated",
                "authentication failed",
                "unauthorized",
                "login required",
                "please log in",
                "invalid api key",
                "login expired",
                "token expired",
                "oauth token has expired",
                "authentication_error",
                "invalid authentication",
                "invalid token",
                "expired token",
                "unauthenticated",
            ),
            "CLI_AUTH_REQUIRED",
            "CLI authentication is required。CLIで再ログインしてから再試行してください",
            False,
        ),
        (
            (
                "unexpected argument",
                "unknown option",
                "unrecognized option",
                "unknown feature",
                "unrecognized feature",
            ),
            "CLI_CONFIG_UNSUPPORTED",
            "CLI version does not support restricted live inference。SeamとCLIの互換性を確認してください",
            False,
        ),
        (
            (
                "rate limit",
                "rate_limit",
                "quota exceeded",
                "usage limit",
                "usage_limit",
            ),
            "CLI_RATE_LIMIT",
            "CLIの利用上限に達しました。利用枠の回復後に再試行してください",
            True,
        ),
        (
            (
                "invalid_json_schema",
                "invalid schema for response_format",
                "invalid schema for text.format",
            ),
            "CLI_SCHEMA_REJECTED",
            "CLI/LLMがノートの出力形式を拒否しました。Seamの更新が必要です",
            False,
        ),
        (
            (
                "model_not_found",
                "model is not supported",
                "model is not available",
                "not supported when using codex with a chatgpt account",
            ),
            "CLI_MODEL_UNAVAILABLE",
            "選択モデルをこのCLI/アカウントで利用できません。次の会議用モデル設定を確認してください",
            False,
        ),
        (
            (
                "failed to initialize in-process app-server client",
                "unable to open database file",
                "operation not permitted",
            ),
            "CLI_LOCAL_RUNTIME",
            "CLIのローカル状態DB・実行環境を初期化できません。実行権限を確認してください",
            False,
        ),
        (
            (
                "connection refused",
                "connection reset",
                "failed to connect",
                "network is unreachable",
                "error sending request",
                "failed to lookup address",
            ),
            "CLI_CONNECTION_FAILED",
            "CLIの接続に失敗しました。ネットワークを確認して再試行してください",
            True,
        ),
    ):
        if any(marker in low for marker in markers):
            return LiveProviderError(message, retryable, code=code)
    return LiveProviderError(
        f"CLI exited unsuccessfully ({exit_code})"
        if exit_code is not None
        else "CLIが推論エラーを返しました",
        code="CLI_EXEC_FAILED",
    )


def reject_tool_event(event: Any) -> None:
    """Inspect protocol fields, never interpret meeting text as a tool event."""
    if not isinstance(event, dict):
        return
    kind = str(event.get("type", ""))
    if (
        kind
        in {
            "command_execution",
            "file_change",
            "file_read",
            "web_search",
            "mcp_tool_call",
            "collab_tool_call",
            "tool_use",
            "tool_result",
            "function_call",
            "custom_tool_call",
            "computer_call",
            "browser_call",
            "image_generation_call",
        }
        or "_tool_" in kind
        or kind.endswith("_tool_call")
    ):
        raise LiveProviderError(
            "CLI attempted a prohibited tool operation", retryable=False
        )
    item = event.get("item")
    if isinstance(item, dict) and str(event.get("type", "")).startswith("item."):
        if item.get("type") not in {"agent_message", "reasoning", "error"}:
            raise LiveProviderError(
                "CLI emitted an unsupported tool/item type", retryable=False
            )
    for key in ("item", "message", "delta", "content_block"):
        reject_tool_event(event.get(key))
    content = event.get("content")
    if isinstance(content, list):
        for block in content:
            reject_tool_event(block)


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            # The launcher may have exited while its child still holds the pipes.
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def run_cli(
    argv: list[str],
    *,
    cwd: str,
    stdin: str = "",
    timeout: float = 10,
    cancellation: asyncio.Event | None = None,
    monitor_events: bool = False,
    environment: dict[str, str] | None = None,
    expected_error: str | None = None,
) -> bytes:
    """Drain both pipes concurrently and kill the entire process tree on failure."""
    if cancellation is not None and cancellation.is_set():
        raise LiveProviderError("Live inference was cancelled")
    data = stdin.encode("utf-8")
    if len(data) > MAX_INPUT_BYTES:
        raise LiveProviderError(
            "Live inference input exceeds the size limit", retryable=False
        )
    # Preserve saved CLI/keychain authentication. Do not forward desktop IPC or
    # daemon addressing variables into the isolated exec process.
    auth_env = {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"}
    env = {
        key: value
        for key, value in (os.environ if environment is None else environment).items()
        if key in auth_env
        or not key.startswith(("CODEX_", "CLAUDE_CODE_", "CLAUDECODE"))
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError) as exc:
        raise LiveProviderError(
            "CLI executable could not be started", retryable=False
        ) from exc

    async def read_bounded(
        stream: asyncio.StreamReader, limit: int, monitor: bool
    ) -> bytes:
        result = bytearray()
        pending = bytearray()
        while chunk := await stream.read(8192):
            result.extend(chunk)
            if len(result) > limit:
                raise LiveProviderError("CLI output exceeds the size limit")
            if monitor:
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, pending = pending.partition(b"\n")
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    reject_tool_event(event)
        if monitor and pending:
            try:
                reject_tool_event(json.loads(pending))
            except (ValueError, UnicodeDecodeError):
                pass
        return bytes(result)

    async def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    async def communicate() -> tuple[bytes, bytes]:
        assert proc.stdout is not None and proc.stderr is not None
        out, err, _, _ = await asyncio.gather(
            read_bounded(proc.stdout, MAX_STDOUT_BYTES, monitor_events),
            read_bounded(proc.stderr, MAX_STDERR_BYTES, False),
            feed(),
            proc.wait(),
        )
        return out, err

    worker = asyncio.create_task(communicate())
    cancel = (
        asyncio.create_task(cancellation.wait()) if cancellation is not None else None
    )
    try:
        done, _ = await asyncio.wait(
            {worker, *([cancel] if cancel else [])},
            timeout=max(0.01, timeout),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel and cancel in done:
            raise LiveProviderError("Live inference was cancelled")
        if worker not in done:
            raise LiveProviderError("CLI inference timed out")
        out, err = await worker
        if (
            expected_error
            and proc.returncode
            and expected_error in err.decode(errors="replace")
        ):
            return b""
        if proc.returncode:
            raise cli_failure(
                (err + out).decode("utf-8", errors="replace"), proc.returncode
            )
        if expected_error:
            raise LiveProviderError(
                "CLIのローカル設定検証が所定の位置で停止しませんでした",
                False,
                code="CLI_PROBE_FAILED",
            )
        return out
    finally:
        await _kill_group(proc)
        worker.cancel()
        if cancel:
            cancel.cancel()
        await asyncio.gather(
            worker, *([cancel] if cancel else []), return_exceptions=True
        )
