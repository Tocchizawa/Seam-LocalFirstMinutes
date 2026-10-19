"""Model-facing schema enforces the same action boundaries as the app harness."""

import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from src.live_notes.models import (
    LiveSettings,
    OperationPlan,
    operation_plan_schema,
    semantic_operation_plan_schema,
    validation_issue_summary,
)
from src.live_notes.repository import LiveNoteStore


@pytest.mark.parametrize("version", [2, 3])
def test_live_cli_schema_requires_every_object_property_including_nullable_fields(version):
    # Strict structured output rejects otherwise-valid JSON Schema when an
    # optional/nullable property is absent from required, even inside $defs.
    def visit(node, path="$"):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, path
                assert set(node.get("required", [])) == set(node["properties"]), path
            for key, value in node.items():
                visit(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}[{index}]")

    visit(semantic_operation_plan_schema(version))


def plan():
    return {
        "schema_version": 1,
        "batch_id": "batch",
        "base_note_revision": 0,
        "operations": [
            {
                "op_id": "batch:1",
                "action": "create_topic",
                "topic_id": "new:1",
                "block_id": None,
                "expected_block_revision": None,
                "title": "認証方式",
                "kind": None,
                "markdown": None,
                "source_segment_seqs": [1],
                "confidence": 0.9,
            },
            {
                "op_id": "batch:2",
                "action": "add_block",
                "topic_id": "new:1",
                "block_id": None,
                "expected_block_revision": None,
                "title": None,
                "kind": "proposal",
                "markdown": "OAuthを検討する",
                "source_segment_seqs": [1],
                "confidence": 0.9,
            },
        ],
    }


def test_valid_topic_and_separate_note_are_accepted_by_schema_and_harness():
    schema = operation_plan_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(plan(), schema)
    assert len(OperationPlan.model_validate(plan()).operations) == 2


@pytest.mark.parametrize(
    "index,field,value",
    [
        (0, "markdown", "merged note"),
        (0, "kind", "proposal"),
        (1, "title", "unexpected heading"),
        (1, "kind", None),
    ],
)
def test_model_schema_rejects_cross_action_fields_before_generation(
    index, field, value
):
    data = plan()
    data["operations"][index][field] = value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, operation_plan_schema())
    with pytest.raises(ValidationError):
        OperationPlan.model_validate(data)


def test_validation_diagnostics_do_not_expose_unknown_fields_or_input():
    data = deepcopy(plan())
    data["operations"][0]["SECRET_USER_TEXT"] = "PRIVATE_INPUT"
    with pytest.raises(ValidationError) as exc:
        OperationPlan.model_validate(data)
    summary = validation_issue_summary(exc.value)
    assert "extra_forbidden" in summary
    assert "SECRET" not in summary and "PRIVATE" not in summary


def test_real_codex_synthetic_response_replays_into_durable_note(tmp_path):
    # Captured from an explicitly authorized synthetic batch; no meeting data.
    raw = json.loads(
        (Path(__file__).parent / "fixtures/live_notes_codex_luna_plan.json").read_text()
    )
    sid = "synthetic-cli-verification"
    store = LiveNoteStore(tmp_path / "real-response.sqlite")
    try:
        store.create(
            sid,
            "synthetic-project",
            "codex",
            {"model": "gpt-6-luna"},
            LiveSettings().model_dump(),
        )
        store.ingest(
            sid,
            [
                {
                    "segment_id": "synthetic-1",
                    "text": "認証方式については、既存のOAuth基盤を利用する案を検討しています。移行期間の確認が必要です。",
                    "start": 0,
                    "end": 15,
                },
                {
                    "segment_id": "synthetic-2",
                    "text": "認証方式は既存のOAuth基盤を利用することで合意し、決定します。田中さんが移行手順を金曜日までに作成します。",
                    "start": 15,
                    "end": 30,
                },
            ],
        )
        batch = store.batch(sid)
        jsonschema.validate(raw, operation_plan_schema())
        snapshot, conflicts = store.apply(sid, batch, store.context(sid, batch), raw)
        assert not conflicts
        assert snapshot["last_processed_segment_seq"] == 2
        assert snapshot["pending_segments"] == 0
        assert snapshot["topics"][0]["title"] == "認証方式"
        assert {block["kind"] for block in snapshot["topics"][0]["blocks"]} == {
            "decision",
            "action",
            "question",
        }
    finally:
        store.close()
