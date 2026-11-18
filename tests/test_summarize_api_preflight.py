"""Summarize API preflight tests.

FastAPI server は起動せず、endpoint 関数を直接呼び出す。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api import summarize as api
from src.summarize.base import ProviderHealth, SummaryErrorCode

PASS = 0
FAIL = 0


def ok(name: str, cond: bool, hint: str = "") -> None:
    global PASS, FAIL
    if cond:
        print(f"  PASS: {name}")
        PASS += 1
    else:
        print(f"  FAIL: {name}{(' — ' + hint) if hint else ''}")
        FAIL += 1


class FakeProvider:
    def __init__(self, health: ProviderHealth) -> None:
        self.health = health

    async def health_check(self) -> ProviderHealth:
        return self.health


class FakeRunner:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str | None]] = []

    def enqueue(self, minutes_id: str, *, provider_name: str | None = None) -> None:
        self.enqueued.append((minutes_id, provider_name))


async def run_case(health: ProviderHealth):
    runner = FakeRunner()
    old_get_minutes = api.db.get_minutes
    old_is_known_provider = api.is_known_provider
    old_is_cloud_provider = api.is_cloud_provider
    old_get_provider = api.get_provider
    old_get_runner = api.get_runner
    try:
        api.db.get_minutes = lambda minutes_id: {"id": minutes_id}  # type: ignore[method-assign]
        api.is_known_provider = lambda _provider: True  # type: ignore[assignment]
        api.is_cloud_provider = lambda _provider: False  # type: ignore[assignment]
        api.get_provider = lambda _provider, _cfg: FakeProvider(health)  # type: ignore[assignment]
        api.get_runner = lambda: runner  # type: ignore[assignment]
        try:
            res = await api.trigger_summarize("m1", provider="fake")
            return res, runner, None
        except HTTPException as e:
            return None, runner, e
    finally:
        api.db.get_minutes = old_get_minutes  # type: ignore[method-assign]
        api.is_known_provider = old_is_known_provider  # type: ignore[assignment]
        api.is_cloud_provider = old_is_cloud_provider  # type: ignore[assignment]
        api.get_provider = old_get_provider  # type: ignore[assignment]
        api.get_runner = old_get_runner  # type: ignore[assignment]


print()
print("=" * 60)
print("  Summarize API preflight")
print("=" * 60)

res, runner, err = asyncio.run(run_case(
    ProviderHealth(
        ok=False,
        code=SummaryErrorCode.OFFLINE.value,
        message="CLI offline",
    ),
))
ok("offline preflight raises HTTPException", isinstance(err, HTTPException))
if isinstance(err, HTTPException):
    ok("offline preflight status 400", err.status_code == 400)
    ok("offline preflight code", err.detail.get("code") == "OFFLINE")
ok("offline preflight does not enqueue", runner.enqueued == [])

res, runner, err = asyncio.run(run_case(
    ProviderHealth(ok=True, code="READY", message="ready", model="fake-1"),
))
ok("ready preflight succeeds", err is None and res is not None)
ok("ready preflight enqueues", runner.enqueued == [("m1", "fake")])

print()
print("=" * 60)
print(f"  結果: {PASS} passed, {FAIL} failed")
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
