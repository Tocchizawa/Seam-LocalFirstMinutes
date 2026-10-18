"""Restricted, one-shot structured inference; no summary-provider dependency."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any


class LiveProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        retryable: bool = True,
        structural: bool = False,
        *,
        code: str = "CLI_ERROR",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.structural = structural
        self.code = code


class StructuredInferenceProvider(ABC):
    name: str

    @abstractmethod
    async def preflight(self) -> dict[str, Any]:
        """Check local executable capabilities, without a paid/model request."""

    @abstractmethod
    async def infer_operations(
        self,
        prompt: str,
        schema: dict[str, Any],
        timeout: float = 120,
        cancellation: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """Return a completed JSON proposal; the harness must validate semantics."""
