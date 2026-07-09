"""Codex CLI provider — OpenAI ChatGPT subscription経由で要約。

`codex exec -` を subprocess で起動し、stdin で prompt を渡して stdout を逐次キャプチャする。
Claude Code 同様 APIキー不要 (Codex CLI のサブスク認証を流用)。

注意:
  - codex は streaming structured output を持たないため、
    ANSI制御文字を除去しつつ stdout をそのまま要約として扱う。
  - prompt は stdin で明示的に渡して close する
    (親プロセスの stdin pipe 継承による `Reading additional input from stdin...` を回避)
"""
from __future__ import annotations

import asyncio
import logging
import re
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

PROVIDER_CODEX = "codex"

# ANSI escape sequences (color, cursor moves など) を除去
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# 行頭の "[YYYY-MM-DDTHH:MM:SS] " 形式タイムスタンプを除去
_TS_PREFIX_RE = re.compile(r"^\[[\d\-T:.Z+]+\]\s*")


def _clean_activity_line(line: str) -> str:
    """codex 進捗ログを UI 表示用に簡潔化。空行や境界線だけの行は破棄。"""
    s = _TS_PREFIX_RE.sub("", line).strip()
    if not s:
        return ""
    # 装飾的な境界線
    if set(s) <= {"-", "=", "_"}:
        return ""
    # 長すぎる行は要約
    if len(s) > 120:
        s = s[:117] + "..."
    return s


class CodexProvider(SummarizerProvider):
    name = PROVIDER_CODEX

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config or {}
        self._binary_path = str(self._config.get("binary_path", "codex"))
        # codex の model 指定方法は --model (codex exec の引数)。
        # alias ("gpt-5", "o3" 等) もフルID もそのまま渡せる前提。
        # 空文字の場合は --model を付けず、Codex CLI 側の既定モデルを使う。
        self._model = str(self._config.get("model", "")).strip()
        self._launcher_command = str(self._config.get("launcher_command", "")).strip()
        self._connect_timeout_sec = clamp_connect_timeout(
            self._config.get("connect_timeout_sec", 12),
        )
        extra = self._config.get("extra_args", [])
        self._extra_args = normalize_extra_args(extra)
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel_event = asyncio.Event()

    def _build_cmd(self, command_args: list[str]) -> tuple[list[str] | None, str]:
        return build_command_argv(
            self._config,
            default_binary=self._binary_path or "codex",
            command_args=command_args,
        )

    @staticmethod
    def _is_chatgpt_model_unsupported(text: str) -> bool:
        low = (text or "").lower()
        return "not supported when using codex with a chatgpt account" in low

    async def health_check(self) -> ProviderHealth:
        version_cmd, launcher_label = self._build_cmd(["--version"])
        if version_cmd is None:
            return ProviderHealth(
                ok=False,
                code=SummaryErrorCode.MODEL_UNAVAILABLE.value,
                message=(
                    f"Codex CLI ('{self._binary_path}') が見つかりません。"
                    "https://github.com/openai/codex からインストールしてください。"
                ),
            )
        try:
            # 1) まず CLI 自体の疎通確認
            proc = await asyncio.create_subprocess_exec(
                *version_cmd,
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
                            "Codex ランチャーコマンドが実行できません。"
                            f" launcher_command='{launcher_label}'"
                        ),
                    )
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.PROVIDER_DOWN.value,
                    message=f"`codex --version` exit {proc.returncode}: {version_err[:200]}",
                )
            version_line = stdout.decode(errors="replace").strip().split("\n")[0]

            # 2) 実行可能性チェック:
            #    `codex exec` を1回走らせ、stdin経由入力・認証・ネットワークなど
            #    実運用で落ちる要因を接続テスト時に検出する。
            async def _run_probe(use_configured_model: bool) -> tuple[int, bytes, bytes] | None:
                model_arg: list[str] = []
                if use_configured_model and self._model:
                    model_arg = ["--model", self._model]
                probe_cmd, _ = self._build_cmd([
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    *model_arg,
                    *self._extra_args,
                    "-",
                ])
                if probe_cmd is None:
                    return None
                probe = await asyncio.create_subprocess_exec(
                    *probe_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert probe.stdin is not None
                probe.stdin.write(b"Reply with exactly OK.\n")
                await probe.stdin.drain()
                probe.stdin.close()
                try:
                    out, err = await asyncio.wait_for(
                        probe.communicate(),
                        timeout=self._connect_timeout_sec,
                    )
                except asyncio.TimeoutError:
                    try:
                        probe.kill()
                    except Exception:
                        pass
                    return None
                return probe.returncode or 0, out, err

            probe_res = await _run_probe(use_configured_model=True)
            if probe_res is None:
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.TIMEOUT.value,
                    message=(
                        "`codex exec` 接続テストが"
                        f"{self._connect_timeout_sec:g}秒以内に完了しませんでした"
                    ),
                )
            probe_rc, _probe_out, probe_err = probe_res
            model_fallback_used = False
            if probe_rc != 0 and self._model:
                err_text = _strip_ansi(probe_err.decode(errors="replace")).strip()
                if self._is_chatgpt_model_unsupported(err_text):
                    fallback_res = await _run_probe(use_configured_model=False)
                    if fallback_res is None:
                        return ProviderHealth(
                            ok=False,
                            code=SummaryErrorCode.TIMEOUT.value,
                            message=(
                                "`codex exec` 接続テストが"
                                f"{self._connect_timeout_sec:g}秒以内に完了しませんでした"
                            ),
                        )
                    probe_rc, _probe_out, probe_err = fallback_res
                    model_fallback_used = probe_rc == 0

            if probe_rc != 0:
                err_text = _strip_ansi(probe_err.decode(errors="replace")).strip()
                excerpt = err_text[-300:] if len(err_text) > 300 else err_text
                code = classify_cli_error_code(err_text)
                return ProviderHealth(
                    ok=False,
                    code=code.value,
                    message=f"`codex exec` exit {probe_rc}: {excerpt}",
                )

            model_label = self._model or "codex-default"
            msg_suffix = ""
            if model_fallback_used:
                model_label = "codex-default"
                msg_suffix = " (model fallback: --model なしで成功)"
            return ProviderHealth(
                ok=True, code="READY",
                message=f"Codex CLI {version_line} (exec OK){msg_suffix}",
                model=model_label,
            )
        except FileNotFoundError:
            return ProviderHealth(
                ok=False,
                code=SummaryErrorCode.MODEL_UNAVAILABLE.value,
                message=f"Codex CLI ('{self._binary_path}') が見つかりません",
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
        timeout_sec: float = 600,
    ) -> SummaryResult:
        sys_p, user_p = build_messages(transcript, project)
        full_prompt = f"{sys_p}\n\n---\n\n{user_p}"

        # ProjectContext から path を取得。
        # repo_path → -C (cwd として agent が触れる primary workspace)
        # doc_dirs → --add-dir (追加で writable=readable にする)
        import os
        repo_arg: list[str] = []
        add_dir_args: list[str] = []
        if project is not None:
            if project.repo_path and os.path.isdir(project.repo_path):
                repo_arg = ["-C", project.repo_path]
            seen: set[str] = set()
            if project.repo_path:
                seen.add(project.repo_path)
            for d in project.doc_dirs:
                if d and os.path.isdir(d) and d not in seen:
                    seen.add(d)
                    add_dir_args.extend(["--add-dir", d])

        def _build_exec_args(with_model: bool) -> list[str]:
            cur_model_arg: list[str] = []
            if with_model and self._model:
                cur_model_arg = ["--model", self._model]
            cmd, _ = self._build_cmd([
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                *cur_model_arg,
                *repo_arg,
                *add_dir_args,
                *self._extra_args,
                "-",
            ])
            if cmd is None:
                raise SummaryError(
                    SummaryErrorCode.MODEL_UNAVAILABLE,
                    f"Codex CLI ('{self._binary_path}') が見つかりません",
                    provider=self.name,
                )
            return cmd

        self._cancel_event.clear()
        started = time.time()

        async def _spawn(with_model: bool) -> asyncio.subprocess.Process:
            args = _build_exec_args(with_model)
            try:
                return await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                raise SummaryError(
                    SummaryErrorCode.MODEL_UNAVAILABLE,
                    f"Codex CLI が見つかりません: {self._binary_path}",
                    provider=self.name,
                )
            except Exception as e:
                raise SummaryError(
                    SummaryErrorCode.UNKNOWN,
                    f"subprocess起動失敗: {e}",
                    provider=self.name,
                )

        self._proc = await _spawn(with_model=True)

        # prompt を stdin に書いて閉じる。
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(full_prompt.encode("utf-8"))
            await self._proc.stdin.drain()
            self._proc.stdin.close()
        except Exception as e:
            await self._kill_proc()
            raise SummaryError(
                SummaryErrorCode.UNKNOWN,
                f"stdin write failed: {e}",
                provider=self.name,
            )

        out_chunks: list[str] = []
        in_summary = False  # 最初の "## " 見出しに当たったら True

        async def _emit_activity(msg: str) -> None:
            if on_activity is None:
                return
            res = on_activity(msg)
            if asyncio.iscoroutine(res):
                await res

        await _emit_activity(f"Codex 起動中 ({self._model})")

        try:
            assert self._proc.stdout is not None
            async with asyncio.timeout(timeout_sec):
                async for raw_line in self._proc.stdout:
                    if self._cancel_event.is_set():
                        await self._kill_proc()
                        raise SummaryError(
                            SummaryErrorCode.CANCELLED,
                            "ユーザーによりキャンセル",
                            provider=self.name,
                        )
                    if not raw_line:
                        continue
                    text = _strip_ansi(raw_line.decode("utf-8", errors="replace"))
                    if not text:
                        continue

                    stripped = text.strip()
                    # markdown 本文の開始は最初の "## " 見出し
                    if not in_summary and stripped.startswith("##"):
                        in_summary = True

                    if not in_summary:
                        # 本文前のログ行は activity として UI に流す。
                        # codex の進捗ログ ("[202...] ..." 形式など) を簡潔化。
                        cleaned = _clean_activity_line(stripped)
                        if cleaned:
                            await _emit_activity(cleaned)
                        continue

                    out_chunks.append(text)
                    if on_token is not None:
                        res = on_token(text)
                        if asyncio.iscoroutine(res):
                            await res
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
                f"Codex generate error: {e}",
                provider=self.name,
            )

        return_code = await self._proc.wait()
        stderr_bytes = b""
        if self._proc.stderr is not None:
            try:
                stderr_bytes = await self._proc.stderr.read()
            except Exception:
                pass

        if return_code != 0 and not out_chunks:
            stderr_text = _strip_ansi(stderr_bytes.decode("utf-8", errors="replace"))[:300]
            if self._model and self._is_chatgpt_model_unsupported(stderr_text):
                try:
                    self._proc = await _spawn(with_model=False)
                except SummaryError:
                    self._proc = None
                    raise
                assert self._proc.stdin is not None
                self._proc.stdin.write(full_prompt.encode("utf-8"))
                await self._proc.stdin.drain()
                self._proc.stdin.close()
                fallback_stdout, fallback_stderr = await self._proc.communicate()
                fallback_code = self._proc.returncode or 0
                self._proc = None
                if fallback_code == 0:
                    fallback_text = _strip_ansi(
                        fallback_stdout.decode("utf-8", errors="replace"),
                    ).strip()
                    duration = time.time() - started
                    return SummaryResult(
                        text=fallback_text,
                        provider=self.name,
                        model="codex-default",
                        input_chars=len(full_prompt),
                        output_chars=len(fallback_text),
                        duration_sec=duration,
                    )
                fallback_err = _strip_ansi(
                    fallback_stderr.decode("utf-8", errors="replace"),
                )
                fallback_code_class = classify_cli_error_code(fallback_err)
                raise SummaryError(
                    fallback_code_class,
                    f"codex exited {fallback_code}: {strip_cli_error(fallback_err)}",
                    provider=self.name,
                )
            self._proc = None
            code = classify_cli_error_code(stderr_text)
            raise SummaryError(
                code,
                f"codex exited {return_code}: {strip_cli_error(stderr_text)}",
                provider=self.name,
            )

        self._proc = None
        text = "".join(out_chunks).strip()
        duration = time.time() - started
        return SummaryResult(
            text=text,
            provider=self.name,
            model="codex-default",
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
        timeout_sec: float = 120,
    ) -> str:
        """`codex exec` で短い prompt を投げてタイトル1行を得る。"""
        from .prompts import build_title_messages

        sys_p, user_p = build_title_messages(transcript, project)
        full_prompt = f"{sys_p}\n\n---\n\n{user_p}"

        model_arg: list[str] = []
        if self._model:
            model_arg = ["--model", self._model]
        args, _ = self._build_cmd([
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            *model_arg,
            *self._extra_args,
            "-",
        ])
        if args is None:
            raise SummaryError(
                SummaryErrorCode.MODEL_UNAVAILABLE,
                f"Codex CLI ('{self._binary_path}') が見つかりません",
                provider=self.name,
            )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None
        try:
            proc.stdin.write(full_prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            raise SummaryError(
                SummaryErrorCode.UNKNOWN,
                f"stdin write failed: {e}",
                provider=self.name,
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_sec,
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
            err_text = _strip_ansi(stderr.decode("utf-8", errors="replace"))
            if self._model and self._is_chatgpt_model_unsupported(err_text):
                fallback_args, _ = self._build_cmd([
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    *self._extra_args,
                    "-",
                ])
                if fallback_args is not None:
                    fallback = await asyncio.create_subprocess_exec(
                        *fallback_args,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    assert fallback.stdin is not None
                    fallback.stdin.write(full_prompt.encode("utf-8"))
                    await fallback.stdin.drain()
                    fallback.stdin.close()
                    try:
                        f_stdout, f_stderr = await asyncio.wait_for(
                            fallback.communicate(), timeout=timeout_sec,
                        )
                    except asyncio.TimeoutError:
                        try:
                            fallback.kill()
                        except Exception:
                            pass
                        raise SummaryError(
                            SummaryErrorCode.TIMEOUT,
                            f"タイトル生成が {timeout_sec}秒以内に終わりませんでした",
                            provider=self.name,
                        )
                    if fallback.returncode == 0:
                        text = _strip_ansi(f_stdout.decode("utf-8", errors="replace"))
                        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                        if not lines:
                            return ""
                        for ln in reversed(lines):
                            if not (ln.startswith("[") and "]" in ln):
                                return ln
                        return lines[-1]
                    err_text = _strip_ansi(f_stderr.decode("utf-8", errors="replace"))
                    code = classify_cli_error_code(err_text)
                    raise SummaryError(
                        code,
                        f"codex exited {fallback.returncode}: {strip_cli_error(err_text, limit=220)}",
                        provider=self.name,
                    )
                else:
                    code = classify_cli_error_code(err_text)
                    raise SummaryError(
                        code,
                        f"codex exited {proc.returncode}: {strip_cli_error(err_text, limit=220)}",
                        provider=self.name,
                    )
            code = classify_cli_error_code(err_text)
            raise SummaryError(
                code,
                f"codex exited {proc.returncode}: {strip_cli_error(err_text, limit=220)}",
                provider=self.name,
            )
        # codex は ANSI 装飾を含むのでそれを除去
        text = _strip_ansi(stdout.decode("utf-8", errors="replace"))
        # 最後の非空行が生成内容 (codex の進捗ログを除去)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return ""
        # 末尾から見て、進捗ログ行 (`[xxxx]` から始まる) を除いた最初の行
        for ln in reversed(lines):
            if not (ln.startswith("[") and "]" in ln):
                return ln
        return lines[-1]

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
