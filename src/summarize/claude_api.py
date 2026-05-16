"""Claude (Anthropic) API provider — クラウド要約。

特徴:
  - 200K context window で長尺会議も single-pass
  - prompt caching で同一systemを使った再要約のコストを 1/10 に
  - HTTP は anthropic SDK の AsyncAnthropic クライアント経由
  - APIキーは macOS Keyring (`seam-app:claude_api_key`) に保存
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .base import (
    PROVIDER_CLAUDE_API,
    ActivityCallback,
    ProjectContext,
    ProviderHealth,
    SummarizerProvider,
    SummaryError,
    SummaryErrorCode,
    SummaryResult,
    TokenCallback,
)
from .prompts import build_messages

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "seam-app"
API_KEY_NAME = "claude_api_key"


def _get_api_key() -> str | None:
    """keyring → 環境変数の優先順で APIキーを取得。"""
    try:
        import keyring

        token = keyring.get_password(KEYRING_SERVICE, API_KEY_NAME)
        if token:
            return token.strip()
    except Exception as e:
        logger.warning("keyring read failed: %s", e)
    env = os.environ.get("ANTHROPIC_API_KEY")
    return env.strip() if env else None


def set_api_key(token: str) -> None:
    """APIキーを keyring に保存。空文字渡された場合は raise。"""
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("empty token")
    import keyring

    keyring.set_password(KEYRING_SERVICE, API_KEY_NAME, cleaned)


def delete_api_key() -> None:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, API_KEY_NAME)
    except Exception:
        pass


def has_api_key() -> bool:
    return bool(_get_api_key())


class ClaudeApiProvider(SummarizerProvider):
    name = PROVIDER_CLAUDE_API

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config or {}
        self._model = str(self._config.get("model", "claude-sonnet-4-6"))
        self._max_tokens = int(self._config.get("max_tokens", 8192))
        self._use_cache = bool(self._config.get("use_prompt_caching", True))
        self._client = None  # 遅延初期化
        self._cancel_event = asyncio.Event()

    def _build_client(self):
        api_key = _get_api_key()
        if not api_key:
            raise SummaryError(
                SummaryErrorCode.AUTH_FAILED,
                "Claude API キーが未設定です。設定画面で登録してください。",
                provider=self.name,
            )
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(api_key=api_key)

    async def health_check(self) -> ProviderHealth:
        if not has_api_key():
            return ProviderHealth(
                ok=False,
                code=SummaryErrorCode.AUTH_FAILED.value,
                message="Claude API キーが未設定です",
            )
        # Anthropic は健康診断専用APIが無いので、極小messageを1トークンで投げて検証
        try:
            from anthropic import APIConnectionError, APIStatusError, AuthenticationError

            client = self._build_client()
            try:
                # 1 token のメッセージで認証チェック
                await client.messages.create(
                    model=self._model,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "."}],
                )
                return ProviderHealth(
                    ok=True, code="READY", message="ok", model=self._model,
                )
            except AuthenticationError:
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.AUTH_FAILED.value,
                    message="APIキーが無効です。設定画面で再登録してください。",
                )
            except APIConnectionError:
                return ProviderHealth(
                    ok=False,
                    code=SummaryErrorCode.OFFLINE.value,
                    message="Anthropic API に接続できません。ネットワークを確認してください。",
                )
            except APIStatusError as e:
                code = SummaryErrorCode.PROVIDER_DOWN
                if e.status_code == 429:
                    code = SummaryErrorCode.RATE_LIMIT
                elif e.status_code in (401, 403):
                    code = SummaryErrorCode.AUTH_FAILED
                return ProviderHealth(
                    ok=False, code=code.value, message=f"API HTTP {e.status_code}: {e}",
                )
            finally:
                try:
                    await client.close()
                except Exception:
                    pass
        except SummaryError as e:
            return ProviderHealth(
                ok=False, code=e.code.value, message=e.message,
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
        timeout_sec: float = 300,
    ) -> SummaryResult:
        _ = on_activity  # 未使用 (API provider は activity を持たない)
        from anthropic import (
            APIConnectionError,
            APIStatusError,
            AuthenticationError,
            RateLimitError,
        )

        sys_p, user_p = build_messages(transcript, project)

        # prompt caching: system prompt は再利用率高いので cache_control 指定。
        # 長い system content をブロック形式で渡し ephemeral cache をマーク。
        if self._use_cache:
            system_blocks: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": sys_p,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_blocks = sys_p  # type: ignore[assignment]

        client = self._build_client()
        self._client = client
        self._cancel_event.clear()
        out_chunks: list[str] = []
        started = time.time()

        try:
            async with asyncio.timeout(timeout_sec):
                async with client.messages.stream(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=system_blocks,
                    messages=[{"role": "user", "content": user_p}],
                ) as stream:
                    async for delta in stream.text_stream:
                        if self._cancel_event.is_set():
                            raise SummaryError(
                                SummaryErrorCode.CANCELLED,
                                "ユーザーによりキャンセル",
                                provider=self.name,
                            )
                        if not delta:
                            continue
                        out_chunks.append(delta)
                        if on_token is not None:
                            res = on_token(delta)
                            if asyncio.iscoroutine(res):
                                await res
        except SummaryError:
            raise
        except AuthenticationError as e:
            raise SummaryError(
                SummaryErrorCode.AUTH_FAILED,
                f"APIキーが無効です: {e}",
                provider=self.name,
            )
        except RateLimitError as e:
            raise SummaryError(
                SummaryErrorCode.RATE_LIMIT,
                f"Rate limit に達しました: {e}",
                provider=self.name,
            )
        except APIConnectionError as e:
            raise SummaryError(
                SummaryErrorCode.OFFLINE,
                f"Anthropic API 接続エラー: {e}",
                provider=self.name,
            )
        except APIStatusError as e:
            code = SummaryErrorCode.PROVIDER_DOWN
            if e.status_code == 429:
                code = SummaryErrorCode.RATE_LIMIT
            elif e.status_code in (401, 403):
                code = SummaryErrorCode.AUTH_FAILED
            raise SummaryError(
                code,
                f"Anthropic API HTTP {e.status_code}: {e}",
                provider=self.name,
            )
        except asyncio.TimeoutError:
            raise SummaryError(
                SummaryErrorCode.TIMEOUT,
                f"生成が {timeout_sec}秒以内に完了しませんでした",
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
                f"Claude API generate error: {e}",
                provider=self.name,
            )
        finally:
            try:
                await client.close()
            except Exception:
                pass
            self._client = None

        text = "".join(out_chunks).strip()
        duration = time.time() - started
        return SummaryResult(
            text=text,
            provider=self.name,
            model=self._model,
            input_chars=len(sys_p) + len(user_p),
            output_chars=len(text),
            duration_sec=duration,
        )

    async def cancel(self, *, reason: str = "user_cancelled") -> None:
        self._cancel_event.set()
        # streaming context manager は次の delta 取得時に CANCELLED を raise する。
        # client.close() は finally で呼ばれる。

    async def generate_title(
        self,
        transcript: str,
        *,
        project: ProjectContext | None = None,
        timeout_sec: float = 60,
    ) -> str:
        from anthropic import (
            APIConnectionError, APIStatusError, AuthenticationError, RateLimitError,
        )
        from .prompts import build_title_messages

        sys_p, user_p = build_title_messages(transcript, project)
        client = self._build_client()
        try:
            resp = await client.messages.create(
                model=self._model,
                max_tokens=80,
                system=sys_p,
                messages=[{"role": "user", "content": user_p}],
            )
            blocks = resp.content or []
            text = "".join(
                getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text"
            )
            return text.strip()
        except AuthenticationError as e:
            raise SummaryError(
                SummaryErrorCode.AUTH_FAILED, str(e), provider=self.name,
            )
        except RateLimitError as e:
            raise SummaryError(
                SummaryErrorCode.RATE_LIMIT, str(e), provider=self.name,
            )
        except APIConnectionError as e:
            raise SummaryError(
                SummaryErrorCode.OFFLINE, str(e), provider=self.name,
            )
        except APIStatusError as e:
            raise SummaryError(
                SummaryErrorCode.PROVIDER_DOWN,
                f"HTTP {e.status_code}", provider=self.name,
            )
        finally:
            try:
                await client.close()
            except Exception:
                pass
