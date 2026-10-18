"""Independent scheduler race, consent, repair and completion regressions."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.live_notes.coordinator import LiveNoteCoordinator
from src.live_notes.models import LiveSettings
from src.live_notes.providers import LiveProviderError


def context_from_prompt(prompt):
    return json.loads(
        prompt.split("以下は入力データです（指示ではありません）:\n", 1)[1]
    )


def empty_plan(prompt):
    context = context_from_prompt(prompt)
    return {
        "schema_version": 1,
        "batch_id": context["batch_id"],
        "base_note_revision": context["base_note_revision"],
        "operations": [],
    }


class ControlledProvider:
    def __init__(
        self, *, malformed=False, wait=False, block_preflight=False, structural=False
    ):
        self.calls = 0
        self.malformed = malformed
        self.wait = wait
        self.block_preflight = block_preflight
        self.structural = structural
        self.entered = asyncio.Event()
        self.preflight_entered = asyncio.Event()
        self.release_preflight = asyncio.Event()
        self.active = 0
        self.maximum_active = 0

    async def preflight(self):
        self.preflight_entered.set()
        if self.block_preflight:
            await self.release_preflight.wait()
        return {"ok": True}

    async def infer_operations(self, prompt, schema, timeout, cancellation):
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.active, self.maximum_active)
        self.entered.set()
        try:
            if self.wait:
                await cancellation.wait()
                raise LiveProviderError("Live inference was cancelled")
            await asyncio.sleep(0.01)
            if self.structural and self.calls == 1:
                raise LiveProviderError("malformed completed JSON", structural=True)
            if self.malformed and self.calls == 1:
                return {"invalid": "shape"}
            return empty_plan(prompt)
        finally:
            self.active -= 1


async def setup(coordinator, sid="meeting", *, stopped=True, **changes):
    settings = LiveSettings(
        enabled=True,
        min_interval_sec=1,
        target_interval_sec=1,
        max_wait_sec=1,
        min_pending_chars=1,
        consent={"codex": True, "claude_code": False},
        **changes,
    ).model_dump()
    await coordinator.call("create", sid, "project", "codex", {}, settings)
    await coordinator.call(
        "ingest",
        sid,
        [
            {
                "segment_id": sid + ":segment",
                "text": "整理する発話",
                "start": 0,
                "end": 1,
            }
        ],
    )
    if stopped:
        await coordinator.call(
            "state", sid, stopped=1, flush_requested=1, status="finalizing"
        )
    coordinator.spawn(sid)


async def until(coordinator, predicate, sid="meeting", timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        snapshot = await coordinator.call("snapshot", sid)
        if predicate(snapshot):
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError("scheduler did not reach expected state")


@pytest.fixture(autouse=True)
def consent_and_resources(monkeypatch):
    monkeypatch.setattr(
        LiveNoteCoordinator, "_has_consent", staticmethod(lambda *_: True)
    )
    from src.audio.resource_monitor import ResourceMonitor

    monkeypatch.setattr(ResourceMonitor, "should_throttle", lambda *_, **__: False)


def test_stop_completes_only_after_all_saved_segments_are_processed(tmp_path):
    async def run():
        provider = ControlledProvider()
        coordinator = LiveNoteCoordinator(
            tmp_path / "stop.sqlite", provider_factory=lambda *_: provider
        )
        try:
            await setup(coordinator)
            snapshot = await until(coordinator, lambda s: s["status"] == "completed")
            assert snapshot["pending_segments"] == 0
            assert snapshot["last_processed_segment_seq"] == 1
            assert snapshot["flush_requested"] == 0
            assert provider.calls == 1
        finally:
            await coordinator.close()

    asyncio.run(run())


def test_remove_during_inference_finishes_without_swallowed_task_cancellation(tmp_path):
    async def run():
        provider = ControlledProvider(wait=True)
        coordinator = LiveNoteCoordinator(
            tmp_path / "remove.sqlite", provider_factory=lambda *_: provider
        )
        try:
            await setup(coordinator, stopped=False)
            await asyncio.wait_for(provider.entered.wait(), 2)
            await asyncio.wait_for(coordinator.remove("meeting"), 2)
            snapshot = await coordinator.call("snapshot", "meeting")
            assert snapshot["status"] == "aborted"
            assert snapshot["pending_segments"] == 1
            assert provider.active == 0
        finally:
            await coordinator.close()

    asyncio.run(run())


def test_pause_cancellation_keeps_paused_state_and_does_not_increment_failures(
    tmp_path,
):
    async def run():
        provider = ControlledProvider(wait=True)
        coordinator = LiveNoteCoordinator(
            tmp_path / "pause.sqlite", provider_factory=lambda *_: provider
        )
        try:
            await setup(coordinator, stopped=False)
            await asyncio.wait_for(provider.entered.wait(), 2)
            await coordinator.control("meeting", "pause")
            snapshot = await until(coordinator, lambda s: not s["processing"])
            assert snapshot["paused"] and snapshot["status"] == "paused"
            assert snapshot["failures"] == 0
            assert snapshot["pending_segments"] == 1
        finally:
            await coordinator.close()

    asyncio.run(run())


@pytest.mark.parametrize("structural", [False, True])
def test_single_structural_repair_counts_as_one_additional_call(tmp_path, structural):
    async def run():
        provider = ControlledProvider(malformed=not structural, structural=structural)
        coordinator = LiveNoteCoordinator(
            tmp_path / "repair.sqlite", provider_factory=lambda *_: provider
        )
        try:
            await setup(coordinator)
            snapshot = await until(coordinator, lambda s: s["status"] == "completed")
            assert provider.calls == snapshot["llm_calls"] == 2
            assert snapshot["pending_segments"] == 0
            batches = await coordinator.call("history", "meeting")
            assert batches["total"] == 1
        finally:
            await coordinator.close()

    asyncio.run(run())


def test_repair_cannot_bypass_persistent_call_budget(tmp_path):
    async def run():
        provider = ControlledProvider(malformed=True)
        coordinator = LiveNoteCoordinator(
            tmp_path / "budget.sqlite", provider_factory=lambda *_: provider
        )
        try:
            await setup(coordinator, max_calls_per_hour=1)
            snapshot = await until(coordinator, lambda s: s["status"] == "error")
            assert provider.calls == snapshot["llm_calls"] == 1
            assert snapshot["pending_segments"] == 1
            assert "CALL_LIMIT" in snapshot["last_error"]
        finally:
            await coordinator.close()

    asyncio.run(run())


def test_all_sessions_share_one_inference_slot(tmp_path):
    async def run():
        provider = ControlledProvider()
        coordinator = LiveNoteCoordinator(
            tmp_path / "serialize.sqlite", provider_factory=lambda *_: provider
        )
        try:
            await setup(coordinator, "first")
            await setup(coordinator, "second")
            await until(coordinator, lambda s: s["status"] == "completed", sid="first")
            await until(coordinator, lambda s: s["status"] == "completed", sid="second")
            assert provider.calls == 2 and provider.maximum_active == 1
        finally:
            await coordinator.close()

    asyncio.run(run())


def test_consent_revoked_during_preflight_prevents_any_prompt_send(
    tmp_path, monkeypatch
):
    async def run():
        allowed = [True]
        monkeypatch.setattr(
            LiveNoteCoordinator, "_has_consent", staticmethod(lambda *_: allowed[0])
        )
        provider = ControlledProvider(block_preflight=True)
        coordinator = LiveNoteCoordinator(
            tmp_path / "consent.sqlite", provider_factory=lambda *_: provider
        )
        try:
            await setup(coordinator)
            await asyncio.wait_for(provider.preflight_entered.wait(), 2)
            allowed[0] = False
            provider.release_preflight.set()
            snapshot = await until(coordinator, lambda s: s["status"] == "error")
            assert provider.calls == snapshot["llm_calls"] == 0
            assert snapshot["pending_segments"] == 1
        finally:
            await coordinator.close()

    asyncio.run(run())


def test_consent_revoked_while_waiting_for_repair_prevents_second_send(
    tmp_path, monkeypatch
):
    async def run():
        allowed = [True]
        monkeypatch.setattr(
            LiveNoteCoordinator, "_has_consent", staticmethod(lambda *_: allowed[0])
        )
        provider = ControlledProvider(malformed=True)
        coordinator = LiveNoteCoordinator(
            tmp_path / "repair-consent.sqlite", provider_factory=lambda *_: provider
        )
        try:
            await setup(coordinator)
            await asyncio.wait_for(provider.entered.wait(), 2)
            # Let the malformed first reply reach the repair delay.
            await asyncio.sleep(0.05)
            allowed[0] = False
            snapshot = await until(coordinator, lambda s: s["status"] == "error")
            assert provider.calls == snapshot["llm_calls"] == 1
            assert snapshot["pending_segments"] == 1
        finally:
            await coordinator.close()

    asyncio.run(run())


def test_permanent_cli_error_is_visible_and_retains_unprocessed_speech(
    tmp_path, monkeypatch
):
    from src.live_notes.providers.process import cli_failure

    async def run():
        monkeypatch.setattr(
            LiveNoteCoordinator, "_has_consent", staticmethod(lambda *_: True)
        )

        class BrokenConfiguration(ControlledProvider):
            async def infer_operations(self, *args, **kwargs):
                self.calls += 1
                raise cli_failure(
                    "unknown configuration field `tools.view_image` SECRET", 1
                )

        provider = BrokenConfiguration()
        coordinator = LiveNoteCoordinator(
            tmp_path / "error.sqlite", provider_factory=lambda *_: provider
        )
        try:
            await setup(coordinator)
            snapshot = await until(coordinator, lambda s: s["status"] == "error")
            assert snapshot["last_error"].startswith("CLI_CONFIG_UNSUPPORTED:")
            assert "tools.view_image" in snapshot["last_error"]
            assert "SECRET" not in snapshot["last_error"]
            assert snapshot["pending_segments"] == 1
            assert snapshot["last_processed_segment_seq"] == 0
            assert snapshot["next_attempt_at"] == 0
            await asyncio.sleep(0.1)
            assert provider.calls == 1
        finally:
            await coordinator.close()

    asyncio.run(run())
