"""Opt-in synthetic-only restricted CLI smoke; one inference call per provider run."""

import asyncio
import os

import pytest

from src.config import config
from src.live_notes.models import (
    SemanticOperationPlan,
    operation_plan_schema,
    resolve_live_provider,
    semantic_operation_plan_schema,
)
from src.live_notes.prompts import build_prompt
from src.live_notes.providers import create_provider


pytestmark = pytest.mark.skipif(
    os.environ.get("SEAM_LIVE_NOTES_V2_REAL_CLI") != "1",
    reason="set SEAM_LIVE_NOTES_V2_REAL_CLI=1 for a synthetic-only CLI call",
)

_CALLS = {"codex": 0, "claude_code": 0}


@pytest.mark.parametrize("provider_name", ["codex", "claude_code"])
def test_restricted_cli_returns_a_valid_v2_plan(provider_name):
    if _CALLS[provider_name] >= 4:
        pytest.fail(f"the four-call {provider_name} verification budget is exhausted")

    settings = config.get("live_notes") or {}
    minutes_ai = config.get("minutes_ai") or {}
    selected = {**settings, "provider": provider_name}
    name, provider_config = resolve_live_provider(selected, minutes_ai)

    async def call_once():
        provider = create_provider(name, provider_config)
        readiness = await provider.preflight()
        if not readiness.get("ok"):
            pytest.skip(f"{name} restricted preflight unavailable: {readiness.get('error', 'unknown')}")
        context = {
            "schema_version": 2,
            "batch_id": f"synthetic-v2-smoke-{name}",
            "batch_kind": "fast",
            "base_revision": 0,
            "base_note_revision": 0,
            "outline": [],
            "related_topics": [],
            "open_questions": [],
            "segments": [{
                "segment_seq": 1,
                "start_ms": 0,
                "end_ms": 1000,
                "speaker_label": "Synthetic",
                "text": "This synthetic test has no product or meeting data. Say no change.",
            }],
        }
        _CALLS[name] += 1
        raw = await provider.infer_operations(
            build_prompt(context), semantic_operation_plan_schema(),
            timeout=float(settings.get("invocation_timeout_sec", 120)),
        )
        plan = SemanticOperationPlan.model_validate(raw)
        assert plan.batch_id == context["batch_id"]
        assert plan.base_revision == 0
        return raw

    raw = asyncio.run(call_once())
    assert raw["schema_version"] == 2
