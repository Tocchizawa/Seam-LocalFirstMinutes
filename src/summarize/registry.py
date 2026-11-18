"""provider name → factory 関数の解決 + auto-detect。

各 provider モジュールは ``create_provider(config) -> SummarizerProvider`` を
公開する。registry はそれらをまとめて提供する。

provider モジュールが import 不能 (依存欠落等) なケースに備え、
factory は遅延参照で解決する。
"""
from __future__ import annotations

import logging
from typing import Callable

from .base import (
    PROVIDER_CLAUDE_API,
    PROVIDER_CLAUDE_CODE,
    PROVIDER_CODEX,
    PROVIDER_GEMINI,
    PROVIDER_OLLAMA,
    PROVIDER_OPENAI,
    V1_PROVIDERS,
    SummarizerProvider,
    SummaryError,
    SummaryErrorCode,
)

logger = logging.getLogger(__name__)


# config dict (= minutes_ai サブツリー) を受け取って provider インスタンスを返す。
ProviderFactory = Callable[[dict], SummarizerProvider]


def _factory_ollama(config: dict) -> SummarizerProvider:
    from .ollama import OllamaProvider

    return OllamaProvider(config.get("ollama", {}))


def _factory_claude_api(config: dict) -> SummarizerProvider:
    from .claude_api import ClaudeApiProvider

    return ClaudeApiProvider(config.get("claude_api", {}))


def _factory_openai(config: dict) -> SummarizerProvider:
    from .openai_api import OpenAIProvider

    return OpenAIProvider(config.get("openai", {}))


def _factory_gemini(config: dict) -> SummarizerProvider:
    from .gemini_api import GeminiProvider

    return GeminiProvider(config.get("gemini", {}))


def _factory_claude_code(config: dict) -> SummarizerProvider:
    from .claude_code import ClaudeCodeProvider

    return ClaudeCodeProvider(config.get("claude_code", {}))


def _factory_codex(config: dict) -> SummarizerProvider:
    from .codex import CodexProvider

    return CodexProvider(config.get("codex", {}))


_FACTORIES: dict[str, ProviderFactory] = {
    PROVIDER_OLLAMA: _factory_ollama,
    PROVIDER_CLAUDE_API: _factory_claude_api,
    PROVIDER_OPENAI: _factory_openai,
    PROVIDER_GEMINI: _factory_gemini,
    PROVIDER_CLAUDE_CODE: _factory_claude_code,
    PROVIDER_CODEX: _factory_codex,
}


def get_provider(name: str, config: dict) -> SummarizerProvider:
    """provider name から実装インスタンスを生成。

    Args:
        name: "ollama" | "claude_api" | "openai" | "gemini"
        config: ``minutes_ai`` サブツリー全体 (provider別設定を含む dict)

    Raises:
        SummaryError(NOT_CONFIGURED): 未知の provider 名
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise SummaryError(
            SummaryErrorCode.NOT_CONFIGURED,
            f"未知のproviderです: {name}",
            provider=name,
        )
    return factory(config)


# ─── auto-detect ─────────────────────────────────────────

def _has_keyring_token(token_key: str) -> bool:
    try:
        import keyring

        # seam-app は pyannote_runner と共用 (HF_TOKEN 等と同居)
        token = keyring.get_password("seam-app", token_key)
        return bool(token)
    except Exception as e:
        logger.warning("keyring read failed (%s): %s", token_key, e)
        return False


async def _check_ollama_reachable(base_url: str) -> tuple[bool, list[str]]:
    """Ollama サーバの疎通確認 + pull済みモデル一覧。"""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/tags")
            if r.status_code != 200:
                return False, []
            data = r.json()
            models = [
                str(m.get("name", ""))
                for m in (data.get("models") or [])
                if m.get("name")
            ]
            return True, models
    except Exception:
        return False, []


def _has_qwen_pulled(models: list[str]) -> bool:
    return any(m.lower().startswith("qwen3") for m in models)


def _has_cli_binary(name: str, cfg: dict | None = None) -> bool:
    from .cli_launcher import build_command_argv

    command, _ = build_command_argv(
        cfg or {"binary_path": name},
        default_binary=name,
        command_args=["--version"],
    )
    return command is not None


async def auto_detect_recommended(
    config: dict,
) -> dict:
    """初回起動 / Settings 初訪問時の推奨 provider 検出。

    優先順位:
      1. Ollama reachable + qwen3 系モデル pull 済み → "ollama"
      2. Claude API key keyring 保存済み → "claude_api"
      3. OpenAI API key keyring 保存済み → "openai"
      4. Gemini API key keyring 保存済み → "gemini"
      5. claude CLI が PATH にある → "claude_code" (subscription前提)
      6. codex CLI が PATH にある → "codex"
      7. Ollama reachable (モデル無し) → "ollama" (要pull案内)
      8. None

    Args:
        config: ``minutes_ai`` サブツリー (Ollama base_url 取得に使う)
    Returns:
        {"provider": str | None, "reason": str}
    """
    ollama_cfg = config.get("ollama", {}) or {}
    base_url = str(ollama_cfg.get("base_url", "http://localhost:11434"))
    reachable, models = await _check_ollama_reachable(base_url)

    if reachable and _has_qwen_pulled(models):
        return {
            "provider": PROVIDER_OLLAMA,
            "reason": "Ollama にQwen3モデルがpull済みです",
        }

    for token_key, provider_name, label in (
        ("claude_api_key", PROVIDER_CLAUDE_API, "Claude API"),
        ("openai_api_key", PROVIDER_OPENAI, "OpenAI (GPT)"),
        ("gemini_api_key", PROVIDER_GEMINI, "Gemini"),
    ):
        if _has_keyring_token(token_key):
            return {
                "provider": provider_name,
                "reason": f"{label} のAPIキーが登録されています",
            }

    if _has_cli_binary("claude", config.get("claude_code", {}) or {}):
        return {
            "provider": PROVIDER_CLAUDE_CODE,
            "reason": "Claude Code CLI が PATH にあります (subscription利用)",
        }
    if _has_cli_binary("codex", config.get("codex", {}) or {}):
        return {
            "provider": PROVIDER_CODEX,
            "reason": "Codex CLI が PATH にあります (subscription利用)",
        }

    if reachable:
        return {
            "provider": PROVIDER_OLLAMA,
            "reason": (
                "Ollama は起動していますがQwen3モデルがpullされていません。"
                "`ollama pull qwen3:8b` でセットアップしてください"
            ),
        }

    return {
        "provider": None,
        "reason": (
            "利用可能なproviderが見つかりません。"
            "Ollama起動 (`ollama serve` + `ollama pull qwen3:8b`)、"
            "クラウドAPIキー登録、または Claude Code/Codex CLI 導入のいずれかを行ってください"
        ),
    }


# ─── consent (cloud初回利用同意) ──────────────────────────

CLOUD_PROVIDERS: tuple[str, ...] = (
    PROVIDER_CLAUDE_API,
    PROVIDER_OPENAI,
    PROVIDER_GEMINI,
)


def is_cloud_provider(name: str) -> bool:
    return name in CLOUD_PROVIDERS


def is_known_provider(name: str) -> bool:
    return name in V1_PROVIDERS
