"""Ollama provider — Qwen3 系モデルでローカル要約。

Ollama HTTP API (`/api/generate`) を streaming モードで叩き、
``response`` フィールドの delta を on_token に流す。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx

from .base import (
    PROVIDER_OLLAMA,
    ActivityCallback,
    ProjectContext,
    ProviderHealth,
    SummarizerProvider,
    SummaryError,
    SummaryErrorCode,
    SummaryResult,
    TokenCallback,
)
from .prompts import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    build_messages,
    validate_context_budget,
)


class OllamaProvider(SummarizerProvider):
    name = PROVIDER_OLLAMA

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config or {}
        self._base_url = str(self._config.get("base_url", "http://localhost:11434")).rstrip("/")
        self._model = str(self._config.get("model", "qwen3:8b"))
        self._num_ctx = int(self._config.get("num_ctx", 8192))
        self._num_thread = int(self._config.get("num_thread", 1))
        self._num_batch = int(self._config.get("num_batch", 8))
        self._num_gpu = int(self._config.get("num_gpu", 0))
        self._low_vram = bool(self._config.get("low_vram", True))
        self._keep_alive_sec = int(self._config.get("keep_alive_sec", 0))
        self._client: httpx.AsyncClient | None = None
        self._cancel_event = asyncio.Event()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=None)  # streaming は手動timeoutで管理
        return self._client

    async def _try_autostart(self) -> bool:
        """OFFLINE 時のフォールバック: ollama serve を自動起動して再試行。

        config の `ollama.auto_start` (top-level、agent 向け設定と共有) が
        true の場合のみ実行する。
        """
        from src.config import config as _cfg
        from .ollama_runtime import ensure_ollama_running

        auto_start = bool(_cfg.get("ollama", "auto_start", default=True))
        if not auto_start:
            return False
        return await ensure_ollama_running(
            base_url=self._base_url, auto_start=True,
        )

    async def health_check(self) -> ProviderHealth:
        # OFFLINE と判定したら自動起動フォールバックを最大1回試す
        for attempt in range(2):
            try:
                client = await self._get_client()
                r = await client.get(f"{self._base_url}/api/tags", timeout=3.0)
                if r.status_code != 200:
                    return ProviderHealth(
                        ok=False,
                        code=SummaryErrorCode.PROVIDER_DOWN.value,
                        message=f"Ollama returned HTTP {r.status_code}",
                    )
                models = r.json().get("models") or []
                names = {str(m.get("name", "")) for m in models}
                if self._model not in names:
                    return ProviderHealth(
                        ok=False,
                        code=SummaryErrorCode.MODEL_UNAVAILABLE.value,
                        message=(
                            f"モデル '{self._model}' が pull されていません。"
                            f"`ollama pull {self._model}` を実行してください。"
                        ),
                        model=self._model,
                    )
                return ProviderHealth(
                    ok=True, code="READY", message="ok", model=self._model,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # 1度目の OFFLINE → 自動起動を試して loop 継続
                if attempt == 0 and await self._try_autostart():
                    continue
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.OFFLINE.value,
                    message=(
                        f"Ollama サーバ ({self._base_url}) に接続できません。"
                        "ollama がインストール済みか確認してください "
                        "(`brew install ollama`)。"
                    ),
                )
            except Exception as e:
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.UNKNOWN.value,
                    message=f"health_check error: {e}",
                )
        # ループは ConnectError + auto_start 失敗時のみ抜けるが、
        # その経路は上の except で return 済みなので到達不能。型解析用にfallback。
        return ProviderHealth(
            ok=False,
            code=SummaryErrorCode.UNKNOWN.value,
            message="health_check loop fell through",
        )

    async def generate(
        self,
        transcript: str,
        *,
        project: ProjectContext | None = None,
        on_token: TokenCallback | None = None,
        on_activity: ActivityCallback | None = None,
        timeout_sec: float = 300,
    ) -> SummaryResult:
        _ = on_activity  # ローカル LLM 経由は activity 表示なし
        # context budget 事前検証
        validate_context_budget(transcript, ctx_window=self._num_ctx)

        sys_p, user_p = build_messages(transcript, project)
        # /no_think で Qwen3 系の thinking モードを抑止する。
        # /api/chat 経由なら chat template が解釈してくれる。
        user_with_directive = user_p + "\n\n/no_think"

        options: dict[str, Any] = {
            "num_ctx": self._num_ctx,
            "num_thread": self._num_thread,
            "num_batch": self._num_batch,
            "num_gpu": self._num_gpu,
            "low_vram": self._low_vram,
            "num_predict": DEFAULT_MAX_OUTPUT_TOKENS,
        }
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": sys_p},
                {"role": "user", "content": user_with_directive},
            ],
            "stream": True,
            "options": options,
            "keep_alive": f"{self._keep_alive_sec}s",
            # /api/chat の think:false は thinking を抑止する (newer ollama)
            "think": False,
        }

        client = await self._get_client()
        self._cancel_event.clear()
        started = time.time()
        out_chunks: list[str] = []
        deadline = started + timeout_sec
        prompt_total_chars = len(sys_p) + len(user_with_directive)

        try:
            async with client.stream(
                "POST", f"{self._base_url}/api/chat", json=body
            ) as resp:
                if resp.status_code != 200:
                    text = await resp.aread()
                    raise SummaryError(
                        SummaryErrorCode.PROVIDER_DOWN,
                        f"Ollama HTTP {resp.status_code}: {text!r}",
                        provider=self.name,
                    )
                async for line in resp.aiter_lines():
                    if self._cancel_event.is_set():
                        raise SummaryError(
                            SummaryErrorCode.CANCELLED,
                            "ユーザーによりキャンセル",
                            provider=self.name,
                        )
                    if time.time() > deadline:
                        raise SummaryError(
                            SummaryErrorCode.TIMEOUT,
                            f"生成が {timeout_sec}秒以内に完了しませんでした",
                            provider=self.name,
                        )
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    # /api/chat streaming: {"message": {"content": "...", "thinking": "..."}, "done": ...}
                    msg = chunk.get("message") or {}
                    delta = str(msg.get("content") or "")
                    # thinking フィールドは捨てる (要約には不要)
                    if delta:
                        out_chunks.append(delta)
                        if on_token is not None:
                            res = on_token(delta)
                            if asyncio.iscoroutine(res):
                                await res
                    if chunk.get("done"):
                        break
        except SummaryError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise SummaryError(
                SummaryErrorCode.OFFLINE,
                f"Ollama に接続できません ({self._base_url})",
                provider=self.name,
            )
        except httpx.ReadTimeout:
            raise SummaryError(
                SummaryErrorCode.TIMEOUT,
                "Ollama 応答がタイムアウトしました",
                provider=self.name,
            )
        except asyncio.CancelledError:
            raise SummaryError(
                SummaryErrorCode.CANCELLED,
                "asyncio cancelled",
                provider=self.name,
            )
        except Exception as e:
            raise SummaryError(
                SummaryErrorCode.UNKNOWN,
                f"Ollama generate error: {e}",
                provider=self.name,
            )

        text = "".join(out_chunks).strip()
        # <think>...</think> ブロックを除去 (古いollama対応)
        text = _strip_think_blocks(text)
        # Qwen3 等は think:false でも reasoning が content に混入することがある。
        # 出力テンプレートの最初の見出し (## 概要) を見つけて、それ以前を捨てる。
        text = _extract_markdown_summary(text)
        duration = time.time() - started
        return SummaryResult(
            text=text,
            provider=self.name,
            model=self._model,
            input_chars=prompt_total_chars,
            output_chars=len(text),
            duration_sec=duration,
        )

    async def cancel(self, *, reason: str = "user_cancelled") -> None:
        self._cancel_event.set()
        # HTTP stream は ``async with`` の外でクローズされるため、フラグだけ立てる

    async def generate_title(
        self,
        transcript: str,
        *,
        project: ProjectContext | None = None,
        timeout_sec: float = 60,
    ) -> str:
        """要約とは別に短いタイトル1行を生成する。"""
        from .prompts import build_title_messages

        sys_p, user_p = build_title_messages(transcript, project)
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": sys_p},
                {"role": "user", "content": user_p + "\n\n/no_think"},
            ],
            "stream": False,
            "options": {
                "num_ctx": self._num_ctx,
                "num_predict": 60,
                "num_gpu": self._num_gpu,
                "low_vram": self._low_vram,
                "num_thread": self._num_thread,
            },
            "keep_alive": f"{self._keep_alive_sec}s",
            "think": False,
        }
        client = await self._get_client()
        try:
            r = await client.post(
                f"{self._base_url}/api/chat", json=body, timeout=timeout_sec,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise SummaryError(
                SummaryErrorCode.OFFLINE,
                f"Ollama に接続できません ({self._base_url})",
                provider=self.name,
            )
        if r.status_code != 200:
            raise SummaryError(
                SummaryErrorCode.PROVIDER_DOWN,
                f"Ollama HTTP {r.status_code}: {r.text[:200]}",
                provider=self.name,
            )
        msg = (r.json() or {}).get("message") or {}
        content = str(msg.get("content") or "")
        # Qwen3 reasoning 混入対策
        content = _strip_think_blocks(content)
        return content.strip()


# ─── helpers ────────────────────────────────────────

_THINK_PATTERN = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)

# 期待出力の最初の見出しパターン (## 概要 / # 概要)
_FIRST_HEADING_PATTERN = re.compile(
    r"^##?\s*(概要|サマリ|Summary|要約)", re.MULTILINE
)

# reasoning 再開の代表パターン (Qwen3 等が markdown 中で reasoning に戻ったとき)
_REASONING_MARKERS = (
    re.compile(r"^(Wait|Hmm|Okay|But the user|Looking at|Let me|Actually|Now,? )",
               re.MULTILINE | re.IGNORECASE),
)


def _strip_think_blocks(text: str) -> str:
    """Qwen3 等が出力する <think>...</think> 思考ブロックを除去。"""
    return _THINK_PATTERN.sub("", text).strip()


def _crop_reasoning_tail(text: str) -> str:
    """段落途中から英文 reasoning が始まったら、その手前で切る。"""
    earliest = len(text)
    for pat in _REASONING_MARKERS:
        m = pat.search(text)
        if m:
            earliest = min(earliest, m.start())
    return text[:earliest].rstrip()


def _extract_markdown_summary(text: str) -> str:
    """thinking モデルの reasoning が混入した場合に Markdown 見出しから抽出。

    1. 最初の `## 概要` までの prefix reasoning を捨てる
    2. 末尾で英文 reasoning が再開している場合は手前で切る
    """
    if not text:
        return text
    m = _FIRST_HEADING_PATTERN.search(text)
    body = text if m is None else text[m.start():]
    body = _crop_reasoning_tail(body)
    return body.strip()
