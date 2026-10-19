"""Live notes use restricted structured adapters, independent of summaries."""

from typing import Any

from .base import LiveProviderError, StructuredInferenceProvider
from .cli import ClaudeLiveProvider, CodexLiveProvider


def create_provider(name: str, config: dict[str, Any]) -> StructuredInferenceProvider:
    providers = {"codex": CodexLiveProvider, "claude_code": ClaudeLiveProvider}
    if name not in providers:
        raise LiveProviderError(
            "Live notes require Codex CLI or Claude Code CLI", retryable=False
        )
    return providers[name](config)


__all__ = [
    "ClaudeLiveProvider",
    "CodexLiveProvider",
    "LiveProviderError",
    "StructuredInferenceProvider",
    "create_provider",
]
