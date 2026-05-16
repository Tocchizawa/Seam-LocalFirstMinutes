"""プロジェクトドキュメントから glossary を LLM で自動抽出する。"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from src.summarize.base import SummaryError, SummaryErrorCode
from src.summarize.cli_launcher import build_command_argv, normalize_extra_args
from .prompts import (
    GLOSSARY_EXTRACT_SYSTEM_PROMPT,
    build_glossary_extract_user_prompt,
    build_glossary_extract_user_prompt_agentic,
    extract_json_object,
)

# CLI provider (claude_code/codex) は docs を Read で自由に読みに行ける
CLI_PROVIDERS = {"claude_code", "codex"}

logger = logging.getLogger(__name__)

# doc_dirs から読み込む際の上限 (LLM のトークン節約)
MAX_DOC_CHARS = 20000
# 各ファイル単体の上限
MAX_PER_FILE_CHARS = 6000
# 対象ファイル拡張子
DOC_EXTENSIONS = {".md", ".txt"}


def collect_docs(doc_dirs: list[str], repo_path: str | None = None) -> str:
    """doc_dirs と repo_path 直下から markdown/txt を再帰収集して連結する。

    優先度: KNOWLEDGE.md / GLOSSARY.md / README.md → その他
    合計 MAX_DOC_CHARS で打ち切り。
    """
    priority_names = ("KNOWLEDGE.md", "GLOSSARY.md", "glossary.md", "README.md")

    candidates: list[Path] = []
    for d in doc_dirs:
        if not d:
            continue
        p = Path(d).expanduser()
        if p.is_dir():
            for ext in DOC_EXTENSIONS:
                candidates.extend(p.rglob(f"*{ext}"))
    if repo_path:
        rp = Path(repo_path).expanduser()
        if rp.is_dir():
            for name in priority_names:
                cand = rp / name
                if cand.is_file():
                    candidates.append(cand)

    # 優先ファイル名を先頭に
    def _priority(p: Path) -> tuple[int, str]:
        return (
            0 if p.name in priority_names else 1,
            str(p),
        )
    candidates = sorted(set(candidates), key=_priority)

    parts: list[str] = []
    total = 0
    for p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("doc read failed (%s): %s", p, e)
            continue
        if not text.strip():
            continue
        snippet = text[:MAX_PER_FILE_CHARS]
        block = f"=== {p.name} ===\n{snippet}\n"
        if total + len(block) > MAX_DOC_CHARS:
            remaining = MAX_DOC_CHARS - total
            if remaining > 200:
                parts.append(block[:remaining])
            break
        parts.append(block)
        total += len(block)
        if total >= MAX_DOC_CHARS:
            break
    return "\n".join(parts)


async def extract_glossary_from_docs(
    project_name: str,
    doc_dirs: list[str],
    repo_path: str | None,
    *,
    provider_name: str,
    ai_cfg: dict,
    timeout_sec: float = 120,
    existing_glossary: list[str] | None = None,
    on_activity: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """プロジェクトドキュメントから glossary 候補を LLM で抽出。

    CLI providers (claude_code/codex) は `--add-dir` でディレクトリ権限のみ与え、
    agent が Read/Glob/Grep で深く探索する。
    API providers (ollama/claude_api/openai) は事前に切り詰めた docs_text を inline で渡す。

    Returns:
        [{"term": "美里子", "description": "..."}, ...]
    """
    is_cli = provider_name in CLI_PROVIDERS
    cli_paths: list[str] = []

    if is_cli:
        # CLI: ディレクトリ存在チェックだけ。中身は agent が読みに行く。
        import os

        if repo_path and os.path.isdir(repo_path):
            cli_paths.append(repo_path)
        for d in doc_dirs:
            if d and os.path.isdir(d) and d not in cli_paths:
                cli_paths.append(d)
        if not cli_paths:
            raise SummaryError(
                SummaryErrorCode.NOT_CONFIGURED,
                "doc_dirs / repo_path が見つかりません。プロジェクト設定を確認してください。",
            )
        user_prompt = build_glossary_extract_user_prompt_agentic(
            project_name, cli_paths, existing_glossary or []
        )
        # CLI は時間をかけて深く読むので timeout を延長 (呼び出し側上書きを尊重)
        if timeout_sec < 600:
            timeout_sec = 600.0
    else:
        # API: 事前に docs を集めて inline
        docs_text = collect_docs(doc_dirs, repo_path)
        if not docs_text.strip():
            raise SummaryError(
                SummaryErrorCode.NOT_CONFIGURED,
                "ドキュメントが見つかりません。doc_dirs / repo_path 配下に .md / .txt を配置してください。",
            )
        user_prompt = build_glossary_extract_user_prompt(docs_text, project_name)

    text = await _call_llm_for_dictionary(
        provider_name=provider_name,
        ai_cfg=ai_cfg,
        system_prompt=GLOSSARY_EXTRACT_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        timeout_sec=timeout_sec,
        cli_paths=cli_paths,
        on_activity=on_activity,
    )

    json_str = extract_json_object(text)
    if not json_str:
        raise SummaryError(
            SummaryErrorCode.OUTPUT_FORMAT,
            f"LLM が JSON を返しませんでした: {text[:200]}",
        )
    try:
        obj = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise SummaryError(
            SummaryErrorCode.OUTPUT_FORMAT,
            f"JSON パース失敗: {e}",
        )
    glossary_raw = obj.get("glossary") or []
    if not isinstance(glossary_raw, list):
        raise SummaryError(
            SummaryErrorCode.OUTPUT_FORMAT,
            "glossary フィールドが list ではありません",
        )

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in glossary_raw:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term", "")).strip()
        desc = str(item.get("description", "")).strip()
        if not term or term in seen:
            continue
        seen.add(term)
        out.append({"term": term, "description": desc})
    return out


# ─── provider 直叩きの簡易 chat API ──────────────────────────
#
# provider.generate は要約に最適化されており、自由なシステムプロンプトを使えない。
# extractor / corrector はそれぞれ専用 system prompt が必要なので、ここで直接 SDK を呼ぶ。
# 対応プロバイダ: ollama / claude_api / openai のみ。CLI agents (claude_code/codex)
# は subprocess で別実装。


async def _call_llm_for_dictionary(
    *,
    provider_name: str,
    ai_cfg: dict,
    system_prompt: str,
    user_prompt: str,
    timeout_sec: float,
    max_tokens: int = 2000,
    cli_paths: list[str] | None = None,
    on_activity: "Callable[[str], None] | None" = None,
) -> str:
    """provider 別に system+user を投げて応答テキストを取得。

    cli_paths は CLI providers (claude_code/codex) のときに `--add-dir` で渡される
    読書権限ディレクトリ。API providers では無視される。
    on_activity は CLI mode のとき、agent の現在処理 (e.g., "Reading README.md") を
    リアルタイムに受け取るためのコールバック。
    """
    if provider_name == "ollama":
        return await _ollama_chat(ai_cfg.get("ollama", {}), system_prompt, user_prompt, timeout_sec, max_tokens)
    if provider_name == "claude_api":
        return await _claude_chat(ai_cfg.get("claude_api", {}), system_prompt, user_prompt, timeout_sec, max_tokens)
    if provider_name == "openai":
        return await _openai_chat(ai_cfg.get("openai", {}), system_prompt, user_prompt, timeout_sec, max_tokens)
    if provider_name == "claude_code":
        return await _claude_code_run(
            ai_cfg.get("claude_code", {}), system_prompt, user_prompt, timeout_sec,
            add_dirs=cli_paths or [],
            on_activity=on_activity,
        )
    if provider_name == "codex":
        return await _codex_run(
            ai_cfg.get("codex", {}), system_prompt, user_prompt, timeout_sec,
            add_dirs=cli_paths or [],
            on_activity=on_activity,
        )
    raise SummaryError(
        SummaryErrorCode.NOT_CONFIGURED,
        f"provider '{provider_name}' は辞書機能で未対応",
    )


async def _ollama_chat(cfg: dict, sys_p: str, user_p: str, timeout: float, max_tokens: int) -> str:
    import httpx

    base_url = str(cfg.get("base_url", "http://localhost:11434")).rstrip("/")
    model = str(cfg.get("model", "qwen3:8b"))
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_p},
            {"role": "user", "content": user_p + "\n\n/no_think"},
        ],
        "stream": False,
        "options": {
            "num_ctx": int(cfg.get("num_ctx", 16384)),
            "num_predict": max_tokens,
            "num_gpu": int(cfg.get("num_gpu", -1)),
        },
        "think": False,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{base_url}/api/chat", json=body)
        if r.status_code != 200:
            raise SummaryError(
                SummaryErrorCode.PROVIDER_DOWN,
                f"Ollama HTTP {r.status_code}: {r.text[:200]}",
            )
        return str((r.json() or {}).get("message", {}).get("content") or "")


async def _claude_chat(cfg: dict, sys_p: str, user_p: str, timeout: float, max_tokens: int) -> str:
    from anthropic import AsyncAnthropic
    from src.summarize.claude_api import _get_api_key

    api_key = _get_api_key()
    if not api_key:
        raise SummaryError(SummaryErrorCode.AUTH_FAILED, "Claude API キー未設定")
    client = AsyncAnthropic(api_key=api_key, timeout=timeout)
    try:
        resp = await client.messages.create(
            model=str(cfg.get("model", "claude-sonnet-4-6")),
            max_tokens=max_tokens,
            system=sys_p,
            messages=[{"role": "user", "content": user_p}],
        )
        blocks = resp.content or []
        return "".join(
            getattr(b, "text", "")
            for b in blocks
            if getattr(b, "type", "") == "text"
        )
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def _openai_chat(cfg: dict, sys_p: str, user_p: str, timeout: float, max_tokens: int) -> str:
    from openai import AsyncOpenAI
    from src.summarize.openai_api import _get_api_key

    api_key = _get_api_key()
    if not api_key:
        raise SummaryError(SummaryErrorCode.AUTH_FAILED, "OpenAI API キー未設定")
    client = AsyncOpenAI(api_key=api_key, timeout=timeout)
    try:
        r = await client.chat.completions.create(
            model=str(cfg.get("model", "gpt-4o-mini")),
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": sys_p},
                {"role": "user", "content": user_p},
            ],
        )
        choice = r.choices[0] if r.choices else None
        return (choice.message.content if choice and choice.message else "") or ""
    finally:
        try:
            await client.close()
        except Exception:
            pass


def _summarize_tool_use(name: str, inp: dict) -> str:
    """tool_use event を 1 行の活動文字列に整形。"""
    name = (name or "").strip() or "tool"
    if name == "Read":
        path = str(inp.get("file_path", "")).strip()
        if path:
            return f"Read: {Path(path).name}"
        return "Read"
    if name == "Glob":
        pattern = str(inp.get("pattern", "")).strip()
        return f"Glob: {pattern}" if pattern else "Glob"
    if name == "Grep":
        pattern = str(inp.get("pattern", "")).strip()
        return f"Grep: {pattern[:40]}" if pattern else "Grep"
    if name == "WebFetch":
        url = str(inp.get("url", "")).strip()
        return f"WebFetch: {url[:60]}" if url else "WebFetch"
    return name


async def _claude_code_run(
    cfg: dict, sys_p: str, user_p: str, timeout: float,
    *,
    add_dirs: list[str] | None = None,
    on_activity: Callable[[str], None] | None = None,
) -> str:
    import asyncio
    import os

    model = str(cfg.get("model", "sonnet")).strip() or "sonnet"
    extra_args = normalize_extra_args(cfg.get("extra_args", []))
    full = f"{sys_p}\n\n---\n\n{user_p}"

    # --add-dir でディレクトリ読書権限を渡し、agent が Read/Glob/Grep で探索可能にする
    add_dir_args: list[str] = []
    if add_dirs:
        seen: set[str] = set()
        for d in add_dirs:
            if d and os.path.isdir(d) and d not in seen:
                seen.add(d)
                add_dir_args.extend(["--add-dir", d])

    # add_dirs があるか on_activity 要求があれば stream-json モードで tool_use を観察できるようにする
    stream_mode = bool(add_dir_args) or on_activity is not None
    args = ["-p", "--no-session-persistence"]
    if stream_mode:
        args.extend(["--output-format", "stream-json", "--verbose"])
    else:
        args.extend(["--output-format", "text"])
    args.extend([
        "--model", model,
        "--input-format", "text",
    ])
    if add_dir_args:
        args.extend(["--allowedTools", "Read,Glob,Grep,WebFetch"])
        args.extend(add_dir_args)
    args.extend(extra_args)
    launch_argv, _ = build_command_argv(
        cfg,
        default_binary="claude",
        command_args=args,
    )
    if launch_argv is None:
        raise SummaryError(
            SummaryErrorCode.MODEL_UNAVAILABLE,
            "Claude Code CLI が見つかりません",
        )

    # stream-json は tool_use / tool_result が巨大ファイル丸ごとを含む場合があるので
    # asyncio StreamReader のデフォルト 64KB を実用上不足しない 1GB に引き上げる
    proc = await asyncio.create_subprocess_exec(
        *launch_argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024 * 1024,
    )

    if not stream_mode:
        try:
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(full.encode("utf-8")), timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise SummaryError(
                SummaryErrorCode.TIMEOUT,
                f"Claude Code が {timeout}秒以内に応答しませんでした",
            )
        if proc.returncode != 0:
            raise SummaryError(
                SummaryErrorCode.PROVIDER_DOWN,
                f"claude exited {proc.returncode}",
            )
        return stdout.decode("utf-8", errors="replace")

    # ─── stream-json mode ───
    if on_activity is not None:
        try:
            on_activity("Claude Code を起動しています...")
        except Exception:
            pass
    assert proc.stdin is not None
    try:
        proc.stdin.write(full.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        raise SummaryError(SummaryErrorCode.UNKNOWN, f"prompt送信失敗: {e}")

    out_text: list[str] = []
    final_result: str | None = None
    assert proc.stdout is not None
    try:
        async with asyncio.timeout(timeout):
            async for line in proc.stdout:
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == "assistant":
                    a_msg = msg.get("message") or {}
                    for block in (a_msg.get("content") or []):
                        bt = block.get("type")
                        if bt == "tool_use":
                            if on_activity is not None:
                                try:
                                    on_activity(_summarize_tool_use(
                                        str(block.get("name", "")),
                                        block.get("input") or {},
                                    ))
                                except Exception:
                                    pass
                        elif bt == "text":
                            text = str(block.get("text") or "")
                            if text:
                                out_text.append(text)
                elif mtype == "result":
                    final_result = str(msg.get("result") or "") or None
                    if on_activity is not None:
                        try:
                            on_activity("用語をまとめています...")
                        except Exception:
                            pass
                    break
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise SummaryError(
            SummaryErrorCode.TIMEOUT,
            f"Claude Code が {timeout}秒以内に応答しませんでした",
        )

    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass

    if proc.returncode not in (0, None):
        raise SummaryError(
            SummaryErrorCode.PROVIDER_DOWN,
            f"claude exited {proc.returncode}",
        )
    return final_result or "".join(out_text)


async def _codex_run(
    cfg: dict, sys_p: str, user_p: str, timeout: float,
    *,
    add_dirs: list[str] | None = None,
    on_activity: Callable[[str], None] | None = None,
) -> str:
    import asyncio
    import os

    model = str(cfg.get("model", "")).strip()
    extra_args = normalize_extra_args(cfg.get("extra_args", []))
    full = f"{sys_p}\n\n---\n\n{user_p}"

    repo_arg: list[str] = []
    add_dir_args: list[str] = []
    if add_dirs:
        seen: set[str] = set()
        # 最初の dir を -C (primary workspace) に、残りを --add-dir に
        for d in add_dirs:
            if not d or not os.path.isdir(d) or d in seen:
                continue
            seen.add(d)
            if not repo_arg:
                repo_arg = ["-C", d]
            else:
                add_dir_args.extend(["--add-dir", d])

    model_arg: list[str] = ["--model", model] if model else []
    args = [
        "exec", "--skip-git-repo-check", "--ephemeral",
        *model_arg,
        *repo_arg,
        *add_dir_args,
        *extra_args,
        "-",
    ]
    launch_argv, _ = build_command_argv(
        cfg,
        default_binary="codex",
        command_args=args,
    )
    if launch_argv is None:
        raise SummaryError(
            SummaryErrorCode.MODEL_UNAVAILABLE,
            "Codex CLI が見つかりません",
        )
    if on_activity is not None:
        try:
            on_activity("Codex を起動しています...")
        except Exception:
            pass
    proc = await asyncio.create_subprocess_exec(
        *launch_argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None
    proc.stdin.write(full.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()
    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise SummaryError(
            SummaryErrorCode.TIMEOUT,
            f"Codex が {timeout}秒以内に応答しませんでした",
        )
    if proc.returncode != 0:
        raise SummaryError(
            SummaryErrorCode.PROVIDER_DOWN,
            f"codex exited {proc.returncode}",
        )
    # ANSI 除去 (codex.py で使ってる正規表現)
    import re
    raw = stdout.decode("utf-8", errors="replace")
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", raw)
