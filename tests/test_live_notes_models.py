"""Live model overrides survive save/restart and never retarget an active meeting."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from src.live_notes.coordinator import LiveNoteCoordinator
from src.live_notes.models import LiveSettings, resolve_live_provider


def test_model_inheritance_and_provider_overrides_do_not_change_summary_settings():
    summary = {
        "provider": "codex",
        "codex": {
            "model": "gpt-6-sol",
            "reasoning_effort": "low",
            "extra_args": ["--unsafe"],
            "api_key": "secret",
        },
        "claude_code": {"model": "sonnet"},
    }
    original = deepcopy(summary)
    defaults = LiveSettings().model_dump()
    assert resolve_live_provider(defaults, summary) == (
        "codex",
        {"model": "gpt-6-sol", "reasoning_effort": "low"},
    )
    settings = LiveSettings(
        models={"codex": " gpt-6-luna ", "claude_code": "haiku"}
    ).model_dump()
    assert resolve_live_provider(settings, summary)[1]["model"] == "gpt-6-luna"
    settings["provider"] = "claude_code"
    assert resolve_live_provider(settings, summary) == (
        "claude_code",
        {"model": "haiku"},
    )
    assert summary == original


@pytest.mark.parametrize("model", ["bad\nmodel", "a" * 161, "bad\x00id", "bad model"])
def test_invalid_model_ids_are_rejected(model):
    with pytest.raises(ValidationError):
        LiveSettings(models={"codex": model})


def test_active_model_is_pinned_and_survives_restart(tmp_path):
    async def run():
        coordinator = LiveNoteCoordinator(tmp_path / "models.db")
        coordinator.spawn = lambda sid: None
        summary = {"provider": "codex", "codex": {"model": "gpt-6-sol"}}
        live = LiveSettings(models={"codex": "gpt-6-luna"}).model_dump()
        try:
            snap = await coordinator.start("meeting", "project", live, summary)
            assert snap["model"] == "gpt-6-luna"
            # Later saved settings must not retarget this existing session.
            live["models"]["codex"] = "another-model"
            summary["codex"]["model"] = "changed-summary-model"
            assert (await coordinator.start("meeting", "project", live, summary))[
                "model"
            ] == "gpt-6-luna"
            assert (await coordinator.start("next-meeting", "project", live, summary))[
                "model"
            ] == "another-model"
        finally:
            await coordinator.close()
        recovered = LiveNoteCoordinator(tmp_path / "models.db")
        try:
            assert (await recovered.call("snapshot", "meeting"))[
                "model"
            ] == "gpt-6-luna"
            session = await recovered.call("session", "meeting")
            assert json.loads(session["provider_config_json"])["model"] == "gpt-6-luna"
        finally:
            await recovered.close()

    asyncio.run(run())


def test_settings_api_merges_per_provider_models_and_preflight_uses_saved_model(
    monkeypatch,
):
    from src.api import live_notes as api_live
    from src.api import settings as api_settings
    from src.live_notes import coordinator as coord_module
    from src.live_notes import providers

    class Config:
        data = {
            "live_notes": LiveSettings(models={"claude_code": "haiku"}).model_dump(),
            "minutes_ai": {"provider": "codex", "codex": {"model": "gpt-6-sol"}},
        }

        def get(self, *keys, default=None):
            value = self.data
            for key in keys:
                if not isinstance(value, dict) or key not in value:
                    return default
                value = value[key]
            return value

        def update(self, data):
            self.data.update(deepcopy(data))

    config = Config()
    monkeypatch.setattr(api_settings, "config", config)
    monkeypatch.setattr(api_live, "config", config)
    monkeypatch.setattr(api_settings, "setup_logging", lambda **kwargs: None)
    monkeypatch.setattr(coord_module, "_coordinator", None)
    seen = []

    class Provider:
        async def preflight(self):
            return {"ok": True}

    def factory(name, cfg):
        seen.append((name, cfg))
        return Provider()

    monkeypatch.setattr(providers, "create_provider", factory)
    app = FastAPI()
    app.include_router(api_settings.router)
    app.include_router(api_live.router)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            result = await client.put(
                "/api/settings",
                json={"live_notes": {"models": {"codex": "gpt-6-luna"}}},
            )
            assert result.status_code == 200, result.text
            saved = (await client.get("/api/settings")).json()
            assert saved["live_notes"]["models"] == {
                "codex": "gpt-6-luna",
                "claude_code": "haiku",
            }
            assert saved["minutes_ai"]["codex"]["model"] == "gpt-6-sol"
            result = await client.get("/api/live-notes/preflight?provider=codex")
            assert result.json()["ok"]
            assert seen[-1] == ("codex", {"model": "gpt-6-luna"})
            result = await client.put(
                "/api/settings", json={"live_notes": {"models": {"codex": "bad model"}}}
            )
            assert result.status_code == 400
            assert config.data["live_notes"]["models"]["codex"] == "gpt-6-luna"

    asyncio.run(run())
