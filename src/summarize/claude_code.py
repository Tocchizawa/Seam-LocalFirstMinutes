"""Claude Code CLI provider — subscription経由で要約。

`claude -p --output-format stream-json --verbose --model <m>` を subprocess で起動し、
JSON streaming events から text_delta を抽出する。

特徴:
  - APIキー不要 (Claude Code subscription を流用)
  - prompt は stdin 経由で渡す (argv 上限を回避)
  - 実行時間: 直 API より遅い (10-30秒、agent loop overhead)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .base import (
    ActivityCallback,
    ProjectContext,
    ProviderHealth,
    SummarizerProvider,
    SummaryError,
    SummaryErrorCode,
    SummaryResult,
    TokenCallback,
)
from .cli_launcher import (
    build_command_argv,
    clamp_connect_timeout,
    classify_cli_error_code,
    is_shell_command_not_found,
    normalize_extra_args,
    strip_cli_error,
)
from .prompts import build_messages

logger = logging.getLogger(__name__)

PROVIDER_CLAUDE_CODE = "claude_code"


class ClaudeCodeProvider(SummarizerProvider):
    """`claude` CLI を subprocess で起動するprovider。"""

    name = PROVIDER_CLAUDE_CODE

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config or {}
        self._binary_path = str(self._config.get("binary_path", "claude"))
        self._model = str(self._config.get("model", "sonnet"))
        # 任意の追加引数 (例: ["--no-mcp"])
        extra = self._config.get("extra_args", [])
        self._extra_args = normalize_extra_args(extra)
        self._launcher_command = str(self._config.get("launcher_command", "")).strip()
        self._connect_timeout_sec = clamp_connect_timeout(
            self._config.get("connect_timeout_sec", 12),
        )
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel_event = asyncio.Event()

    def _build_cmd(self, command_args: list[str]) -> tuple[list[str] | None, str]:
        return build_command_argv(
            self._config,
            default_binary=self._binary_path or "claude",
            command_args=command_args,
        )

    async def health_check(self) -> ProviderHealth:
        cmd, launcher_label = self._build_cmd(["--version"])
        if cmd is None:
            return ProviderHealth(
                ok=False,
                code=SummaryErrorCode.MODEL_UNAVAILABLE.value,
                message=(
                    f"Claude Code CLI ('{self._binary_path}') が見つかりません。"
                    "https://claude.com/code からインストールしてください。"
                ),
            )
        # `claude --version` で動作確認 (5秒以内)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.TIMEOUT.value,
                    message=f"`{self._binary_path} --version` が5秒以内に応答しませんでした",
                )
            if proc.returncode != 0:
                version_err = stderr.decode(errors="replace")
                if self._launcher_command and is_shell_command_not_found(version_err):
                    return ProviderHealth(
                        ok=False,
                        code=SummaryErrorCode.MODEL_UNAVAILABLE.value,
                        message=(
                            "Claude Code ランチャーコマンドが実行できません。"
                            f" launcher_command='{launcher_label}'"
                        ),
                    )
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.PROVIDER_DOWN.value,
                    message=f"`claude --version` exit {proc.returncode}: {version_err[:200]}",
                )
            version_line = stdout.decode(errors="replace").strip().split("\n")[0]

            probe_args = [
                "-p",
                "--output-format", "text",
                "--model", self._model,
                "--input-format", "text",
                "--no-session-persistence",
                *self._extra_args,
            ]
            probe_cmd, _ = self._build_cmd(probe_args)
            if probe_cmd is None:
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.MODEL_UNAVAILABLE.value,
                    message=f"Claude Code CLI ('{self._binary_path}') が見つかりません。",
                )
            probe = await asyncio.create_subprocess_exec(
                *probe_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                probe_out, probe_err = await asyncio.wait_for(
                    probe.communicate(b"Reply with exactly OK.\n"),
                    timeout=self._connect_timeout_sec,
                )
            except asyncio.TimeoutError:
                try:
                    probe.kill()
                except Exception:
                    pass
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.TIMEOUT.value,
                    message=(
                        "`claude -p` 接続テストが"
                        f"{self._connect_timeout_sec:g}秒以内に完了しませんでした"
                    ),
                )
            if probe.returncode != 0:
                err_text = (
                    probe_err.decode(errors="replace")
                    or probe_out.decode(errors="replace")
                )
                if self._launcher_command and is_shell_command_not_found(err_text):
                    return ProviderHealth(
                        ok=False,
                        code=SummaryErrorCode.MODEL_UNAVAILABLE.value,
                        message=(
                            "Claude Code ランチャーコマンドが実行できません。"
                            f" launcher_command='{launcher_label}'"
                        ),
                    )
                code = classify_cli_error_code(err_text)
                return ProviderHealth(
                    ok=False,
                    code=code.value,
                    message=f"`claude -p` 接続失敗: {strip_cli_error(err_text)}",
                )
            return ProviderHealth(
                ok=True, code="READY",
                message=f"Claude Code CLI {version_line} (prompt OK)",
                model=self._model,
            )
        except Exception as e:
            return ProviderHealth(
                ok=False,
                code=SummaryErrorCode.UNKNOWN.value,
                message=f"health_check error: {e}",
            )

    async def generate(
        self,
        transcript: str,
        *,
        project: ProjectContext | None = None,
        on_token: TokenCallback | None = None,
        on_activity: ActivityCallback | None = None,
        timeout_sec: float = 600,  # CLI は遅い前提でデフォルト10分
    ) -> SummaryResult:
        cmd, _ = self._build_cmd(["--version"])
        if cmd is None:
            raise SummaryError(
                SummaryErrorCode.MODEL_UNAVAILABLE,
                f"Claude Code CLI ('{self._binary_path}') が見つかりません",
                provider=self.name,
            )
        sys_p, user_p = build_messages(transcript, project)
        # Claude Code は --append-system-prompt があるが、--print 経由では prompt が
        # ユーザー入力の位置づけ。system prompt + user prompt を1つに連結して渡す。
        full_prompt = f"{sys_p}\n\n---\n\n{user_p}"

        # ProjectContext.repo_path / doc_dirs を --add-dir で渡し、agent が
        # Read/Grep tool で自由に参照できるようにする。存在しないパスは弾く。
        import os
        add_dirs: list[str] = []
        if project is not None:
            if project.repo_path and os.path.isdir(project.repo_path):
                add_dirs.append(project.repo_path)
            for d in project.doc_dirs:
                if d and os.path.isdir(d) and d not in add_dirs:
                    add_dirs.append(d)
        add_dir_args: list[str] = []
        for d in add_dirs:
            add_dir_args.extend(["--add-dir", d])

        args = [
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            # Token単位でのstream_eventを受け取るため。これが無いと
            # 終了時に1度だけ assistant event が来るのみで streaming にならない。
            "--include-partial-messages",
            "--model", self._model,
            "--input-format", "text",
            # 要約は one-shot なので Claude Code 側に履歴を残さない。
            "--no-session-persistence",
            # -p モードはデフォルトで permission-mode=default のため、ツール実行が
            # 全て拒否されてしまう (= --add-dir で渡しても agent が読めない)。
            # 要約で必要な readonly tool だけを明示許可する (Edit/Write は許可しない)。
            "--allowedTools", "Read,Glob,Grep,WebFetch",
            *add_dir_args,
            *self._extra_args,
        ]
        launch_argv, _ = self._build_cmd(args)
        if launch_argv is None:
            raise SummaryError(
                SummaryErrorCode.MODEL_UNAVAILABLE,
                f"Claude Code CLI ('{self._binary_path}') が見つかりません",
                provider=self.name,
            )

        self._cancel_event.clear()
        started = time.time()

        try:
            # stream-json は tool_use / tool_result が巨大ファイルを含むことがあるので 1GB に設定
            self._proc = await asyncio.create_subprocess_exec(
                *launch_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024 * 1024,
            )
        except FileNotFoundError:
            raise SummaryError(
                SummaryErrorCode.MODEL_UNAVAILABLE,
                f"Claude Code CLI が見つかりません: {self._binary_path}",
                provider=self.name,
            )
        except Exception as e:
            raise SummaryError(
                SummaryErrorCode.UNKNOWN,
                f"subprocess起動失敗: {e}",
                provider=self.name,
            )

        # stdin に prompt を書いて閉じる
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(full_prompt.encode("utf-8"))
            await self._proc.stdin.drain()
            self._proc.stdin.close()
        except Exception as e:
            await self._kill_proc()
            raise SummaryError(
                SummaryErrorCode.UNKNOWN,
                f"prompt送信失敗: {e}",
                provider=self.name,
            )

        out_chunks: list[str] = []
        actual_model: str | None = None
        # stream_event が来ない環境向けのフォールバック: assistant message の
        # content[*].text の全文から差分を計算して on_token に流す。
        accumulated_full = ""

        async def _emit_activity(msg_text: str) -> None:
            if on_activity is None:
                return
            res = on_activity(msg_text)
            if asyncio.iscoroutine(res):
                await res

        await _emit_activity(f"Claude Code を起動中 ({self._model})")

        try:
            assert self._proc.stdout is not None
            async with asyncio.timeout(timeout_sec):
                async for line in self._proc.stdout:
                    if self._cancel_event.is_set():
                        await self._kill_proc()
                        raise SummaryError(
                            SummaryErrorCode.CANCELLED,
                            "ユーザーによりキャンセル",
                            provider=self.name,
                        )
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", errors="replace"))
                    except Exception:
                        continue

                    mtype = msg.get("type")
                    if mtype == "system" and msg.get("subtype") == "init":
                        actual_model = str(msg.get("model") or self._model)
                        await _emit_activity(f"接続: {actual_model}")
                    elif mtype == "stream_event":
                        evt = msg.get("event") or {}
                        et = evt.get("type")
                        if et == "content_block_delta":
                            delta = evt.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                text = str(delta.get("text") or "")
                                if text:
                                    out_chunks.append(text)
                                    accumulated_full += text
                                    if on_token is not None:
                                        res = on_token(text)
                                        if asyncio.iscoroutine(res):
                                            await res
                        elif et == "content_block_start":
                            block = evt.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                tool_name = str(block.get("name") or "tool")
                                tinp = block.get("input") or {}
                                hint = (
                                    tinp.get("file_path")
                                    or tinp.get("path")
                                    or tinp.get("pattern")
                                    or tinp.get("url")
                                    or ""
                                )
                                label = f"{tool_name}: {hint}" if hint else tool_name
                                await _emit_activity(label[:120])
                    elif mtype == "assistant":
                        # フォールバック: stream_event が無い環境向け。
                        # ここでまだ out_chunks が空なら assistant の content から取る。
                        a_msg = msg.get("message") or {}
                        blocks = a_msg.get("content") or []
                        full = "".join(
                            str(b.get("text") or "")
                            for b in blocks if b.get("type") == "text"
                        )
                        if full and full.startswith(accumulated_full):
                            delta = full[len(accumulated_full):]
                            if delta:
                                out_chunks.append(delta)
                                accumulated_full = full
                                if on_token is not None:
                                    res = on_token(delta)
                                    if asyncio.iscoroutine(res):
                                        await res
                        elif full and not accumulated_full:
                            # stream_event無しで一気に来たケース
                            out_chunks.append(full)
                            accumulated_full = full
                            if on_token is not None:
                                res = on_token(full)
                                if asyncio.iscoroutine(res):
                                    await res
                    elif mtype == "result":
                        # 終了マーカー
                        break
        except SummaryError:
            await self._kill_proc()
            raise
        except asyncio.TimeoutError:
            await self._kill_proc()
            raise SummaryError(
                SummaryErrorCode.TIMEOUT,
                f"生成が {timeout_sec}秒以内に完了しませんでした",
                provider=self.name,
            )
        except asyncio.CancelledError:
            await self._kill_proc()
            raise SummaryError(
                SummaryErrorCode.CANCELLED,
                "asyncio cancelled",
                provider=self.name,
            )
        except Exception as e:
            await self._kill_proc()
            raise SummaryError(
                SummaryErrorCode.UNKNOWN,
                f"Claude Code generate error: {e}",
                provider=self.name,
            )

        # プロセスの終了確認 + stderr 取得 (失敗診断用)
        return_code = await self._proc.wait()
        stderr_bytes = b""
        if self._proc.stderr is not None:
            try:
                stderr_bytes = await self._proc.stderr.read()
            except Exception:
                pass
        self._proc = None

        if return_code != 0 and not out_chunks:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:300]
            code = classify_cli_error_code(stderr_text)
            raise SummaryError(
                code,
                f"claude exited {return_code}: {strip_cli_error(stderr_text)}",
                provider=self.name,
            )

        text = "".join(out_chunks).strip()
        duration = time.time() - started
        return SummaryResult(
            text=text,
            provider=self.name,
            model=actual_model or self._model,
            input_chars=len(full_prompt),
            output_chars=len(text),
            duration_sec=duration,
        )

    async def cancel(self, *, reason: str = "user_cancelled") -> None:
        self._cancel_event.set()
        await self._kill_proc()

    async def generate_title(
        self,
        transcript: str,
        *,
        project: ProjectContext | None = None,
        timeout_sec: float = 90,
    ) -> str:
        """`claude -p --output-format text` で短い prompt を投げてタイトル1行を得る。

        要約と違い stream-json ではなく ``--output-format text`` で結果のみ取得。
        """
        from .prompts import build_title_messages

        sys_p, user_p = build_title_messages(transcript, project)
        full_prompt = f"{sys_p}\n\n---\n\n{user_p}"

        args = [
            "-p",
            "--output-format", "text",
            "--model", self._model,
            "--input-format", "text",
            "--no-session-persistence",
            *self._extra_args,
        ]
        launch_argv, _ = self._build_cmd(args)
        if launch_argv is None:
            raise SummaryError(
                SummaryErrorCode.MODEL_UNAVAILABLE,
                f"Claude Code CLI ('{self._binary_path}') が見つかりません",
                provider=self.name,
            )
        proc = await asyncio.create_subprocess_exec(
            *launch_argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(full_prompt.encode("utf-8")),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise SummaryError(
                SummaryErrorCode.TIMEOUT,
                f"タイトル生成が {timeout_sec}秒以内に終わりませんでした",
                provider=self.name,
            )
        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace")
            code = classify_cli_error_code(err_text)
            raise SummaryError(
                code,
                f"claude exited {proc.returncode}: "
                f"{strip_cli_error(err_text, limit=200)}",
                provider=self.name,
            )
        return stdout.decode("utf-8", errors="replace").strip()

    async def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("kill_proc error: %s", e)
