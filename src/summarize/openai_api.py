"""OpenAI (GPT) API provider — クラウド要約。

特徴:
  - Chat Completions の streaming で token を逐次取得
  - APIキーは macOS Keyring (`seam-app:openai_api_key`) に保存
  - エラーは SummaryErrorCode に正規化
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .base import (
    PROVIDER_OPENAI,
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
API_KEY_NAME = "openai_api_key"


def _get_api_key() -> str | None:
    try:
        import keyring

        token = keyring.get_password(KEYRING_SERVICE, API_KEY_NAME)
        if token:
            return token.strip()
    except Exception as e:
        logger.warning("keyring read failed: %s", e)
    env = os.environ.get("OPENAI_API_KEY")
    return env.strip() if env else None


def set_api_key(token: str) -> None:
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


class OpenAIProvider(SummarizerProvider):
    name = PROVIDER_OPENAI

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config or {}
        self._model = str(self._config.get("model", "gpt-4o-mini"))
        self._max_tokens = int(self._config.get("max_tokens", 8192))
        self._client = None
        self._cancel_event = asyncio.Event()

    def _build_client(self):
        api_key = _get_api_key()
        if not api_key:
            raise SummaryError(
                SummaryErrorCode.AUTH_FAILED,
                "OpenAI API キーが未設定です。設定画面で登録してください。",
                provider=self.name,
            )
        from openai import AsyncOpenAI

        return AsyncOpenAI(api_key=api_key)

    async def health_check(self) -> ProviderHealth:
        if not has_api_key():
            return ProviderHealth(
                ok=False,
                code=SummaryErrorCode.AUTH_FAILED.value,
                message="OpenAI API キーが未設定です",
            )
        try:
            from openai import APIConnectionError, APIStatusError, AuthenticationError

            client = self._build_client()
            try:
                # 1 token 投げて認証チェック
                await client.chat.completions.create(
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
                    message="OpenAI API に接続できません。ネットワークを確認してください。",
                )
            except APIStatusError as e:
                code = SummaryErrorCode.PROVIDER_DOWN
                if e.status_code == 429:
                    code = SummaryErrorCode.RATE_LIMIT
                elif e.status_code in (401, 403):
                    code = SummaryErrorCode.AUTH_FAILED
                elif e.status_code == 404:
                    # model not found
                    code = SummaryErrorCode.MODEL_UNAVAILABLE
                return ProviderHealth(
                    ok=False, code=code.value,
                    message=f"OpenAI API HTTP {e.status_code}: {e}",
                )
            finally:
                try:
                    await client.close()
                except Exception:
                    pass
        except SummaryError as e:
            return ProviderHealth(ok=False, code=e.code.value, message=e.message)
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
        _ = on_activity  # API provider は activity を持たない
        from openai import (
            APIConnectionError,
            APIStatusError,
            AuthenticationError,
            RateLimitError,
        )

        sys_p, user_p = build_messages(transcript, project)
        client = self._build_client()
        self._client = client
        self._cancel_event.clear()
        out_chunks: list[str] = []
        started = time.time()

        try:
            async with asyncio.timeout(timeout_sec):
                stream = await client.chat.completions.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    messages=[
                        {"role": "system", "content": sys_p},
                        {"role": "user", "content": user_p},
                    ],
                    stream=True,
                )
                async for event in stream:
                    if self._cancel_event.is_set():
                        try:
                            await stream.close()
                        except Exception:
                            pass
                        raise SummaryError(
                            SummaryErrorCode.CANCELLED,
                            "ユーザーによりキャンセル",
                            provider=self.name,
                        )
                    try:
                        choices = event.choices
                    except AttributeError:
                        continue
                    if not choices:
                        continue
                    delta_obj = getattr(choices[0], "delta", None)
                    if delta_obj is None:
                        continue
                    delta_text = getattr(delta_obj, "content", None)
                    if not delta_text:
                        continue
                    out_chunks.append(delta_text)
                    if on_token is not None:
                        res = on_token(delta_text)
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
                f"OpenAI API 接続エラー: {e}",
                provider=self.name,
            )
        except APIStatusError as e:
            code = SummaryErrorCode.PROVIDER_DOWN
            if e.status_code == 429:
                code = SummaryErrorCode.RATE_LIMIT
            elif e.status_code in (401, 403):
                code = SummaryErrorCode.AUTH_FAILED
            elif e.status_code == 404:
                code = SummaryErrorCode.MODEL_UNAVAILABLE
            raise SummaryError(
                code,
                f"OpenAI API HTTP {e.status_code}: {e}",
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
                f"OpenAI generate error: {e}",
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

    async def generate_title(
        self,
        transcript: str,
        *,
        project: ProjectContext | None = None,
        timeout_sec: float = 60,
    ) -> str:
        from openai import (
            APIConnectionError, APIStatusError, AuthenticationError, RateLimitError,
        )
        from .prompts import build_title_messages

        sys_p, user_p = build_title_messages(transcript, project)
        client = self._build_client()
        try:
            r = await client.chat.completions.create(
                model=self._model,
                max_tokens=80,
                messages=[
                    {"role": "system", "content": sys_p},
                    {"role": "user", "content": user_p},
                ],
            )
            choice = r.choices[0] if r.choices else None
            if choice is None or not choice.message:
                return ""
            return (choice.message.content or "").strip()
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
