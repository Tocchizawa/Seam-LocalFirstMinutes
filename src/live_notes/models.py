from __future__ import annotations

import re
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KINDS = ("context", "point", "proposal", "concern", "decision", "question", "action")
ACTION_FIELDS = {
    "create_topic": {"topic_id", "title"},
    "add_block": {"topic_id", "kind", "markdown"},
    "update_block": {
        "topic_id",
        "block_id",
        "expected_block_revision",
        "kind",
        "markdown",
    },
    "rename_topic": {"topic_id", "title"},
    "set_active_topic": {"topic_id"},
    "no_op": set(),
}
DEFAULTS = dict(
    enabled=False,
    provider="inherit",
    models={"codex": "", "claude_code": ""},
    min_interval_sec=20,
    target_interval_sec=35,
    max_wait_sec=60,
    min_pending_chars=120,
    max_batch_chars=6000,
    max_operations_per_batch=20,
    max_calls_per_hour=90,
    invocation_timeout_sec=120,
    max_structural_repair_calls=1,
    debounce_edit_ms=500,
    preserve_human_edits=True,
    consent={"codex": False, "claude_code": False},
)


class LiveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    provider: Literal["inherit", "codex", "claude_code"] = "inherit"
    models: dict[Literal["codex", "claude_code"], str] = Field(
        default_factory=lambda: {"codex": "", "claude_code": ""}
    )
    min_interval_sec: float = Field(default=20, ge=1, le=3600)
    target_interval_sec: float = Field(default=35, ge=1, le=3600)
    max_wait_sec: float = Field(default=60, ge=1, le=7200)
    min_pending_chars: int = Field(default=120, ge=1, le=6000)
    max_batch_chars: int = Field(default=6000, ge=120, le=6000)
    max_operations_per_batch: int = Field(default=20, ge=1, le=20)
    max_calls_per_hour: int = Field(default=90, ge=1, le=360)
    invocation_timeout_sec: float = Field(default=120, ge=1, le=300)
    max_structural_repair_calls: int = Field(default=1, ge=0, le=1)
    debounce_edit_ms: int = Field(default=500, ge=100, le=2000)
    preserve_human_edits: Literal[True] = True
    consent: dict[Literal["codex", "claude_code"], bool] = Field(
        default_factory=lambda: {"codex": False, "claude_code": False}
    )

    @field_validator("models")
    @classmethod
    def model_ids(cls, value):
        result = {"codex": "", "claude_code": ""}
        for provider, model in value.items():
            model = model.strip()
            if len(model) > 160 or any(ch.isspace() or ord(ch) < 32 for ch in model):
                raise ValueError(
                    "model IDs must be at most 160 characters without whitespace"
                )
            result[provider] = model
        return result

    @model_validator(mode="after")
    def intervals(self):
        if not self.min_interval_sec <= self.target_interval_sec <= self.max_wait_sec:
            raise ValueError(
                "min_interval_sec <= target_interval_sec <= max_wait_sec が必要です"
            )
        if self.min_pending_chars > self.max_batch_chars:
            raise ValueError("min_pending_chars must not exceed max_batch_chars")
        return self


def resolve_live_provider(settings: dict, minutes_ai: dict) -> tuple[str, dict]:
    """Resolve safe CLI configuration once; callers persist it at meeting start."""
    name = settings.get("provider", "inherit")
    if name == "inherit":
        name = minutes_ai.get("provider", "")
    source = minutes_ai.get(name, {}) if name in ("codex", "claude_code") else {}
    cfg = {
        key: source[key]
        for key in (
            "binary_path",
            "model",
            "reasoning_effort",
            "launcher_command",
            "launcher_shell",
            "launcher_interactive",
            "connect_timeout_sec",
        )
        if key in source
    }
    model = settings.get("models", {}).get(name, "")
    if model:
        cfg["model"] = model
    return name, cfg


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    op_id: str = Field(min_length=1, max_length=160)
    action: Literal[
        "create_topic",
        "add_block",
        "update_block",
        "rename_topic",
        "set_active_topic",
        "no_op",
    ]
    topic_id: str | None
    block_id: str | None
    expected_block_revision: int | None = Field(ge=0)
    title: str | None = Field(max_length=120)
    kind: (
        Literal[
            "context", "point", "proposal", "concern", "decision", "question", "action"
        ]
        | None
    )
    markdown: str | None = Field(max_length=2000)
    source_segment_seqs: list[int] = Field(max_length=1000)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def action_fields(self):
        fields = {
            "topic_id",
            "block_id",
            "expected_block_revision",
            "title",
            "kind",
            "markdown",
        }
        required = ACTION_FIELDS[self.action]
        for name in fields:
            value = getattr(self, name)
            if name not in required and value is not None:
                raise ValueError(f"{self.action}: unused {name} must be null")
            if name in required and (
                value is None or isinstance(value, str) and not value.strip()
            ):
                raise ValueError(f"{self.action}: {name} is required")
        # Evidence membership and required evidence for document edits are
        # checked transactionally by the store against the supplied batch.
        # no_op may cite valid evidence to acknowledge an insignificant utterance.
        return self


class OperationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    batch_id: str = Field(min_length=1, max_length=160)
    base_note_revision: int = Field(ge=0)
    operations: list[Operation] = Field(max_length=20)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        # Literal[1] accepts True and 1.0 through Python equality even when the
        # surrounding model is strict. The wire contract requires an integer.
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


def operation_plan_schema() -> dict:
    """Constrain nullable fields per action in the model-facing schema as well."""
    schema = OperationPlan.model_json_schema()
    base = schema["$defs"]["Operation"]
    variants = []
    nullable_fields = set().union(*ACTION_FIELDS.values())
    for action, required in ACTION_FIELDS.items():
        variant = deepcopy(base)
        properties = variant["properties"]
        properties["action"] = {"type": "string", "enum": [action]}
        for name in nullable_fields:
            if name not in required:
                properties[name] = {"type": "null"}
            else:
                prop = properties[name]
                prop["anyOf"] = [
                    part for part in prop["anyOf"] if part.get("type") != "null"
                ]
        variants.append(variant)
    schema["$defs"]["Operation"] = {"anyOf": variants}
    return schema


def validation_issue_summary(error) -> str:
    """Only schema paths/types and our fixed validator messages; no input values."""
    known = set(Operation.model_fields) | set(OperationPlan.model_fields)
    issues = []
    for item in error.errors(include_input=False, include_url=False)[:3]:
        path = ".".join(
            str(part) if isinstance(part, int) or part in known else "?"
            for part in item["loc"]
        )
        detail = str(item.get("ctx", {}).get("error", ""))
        if not re.fullmatch(
            r"(?:create_topic|add_block|update_block|rename_topic|set_active_topic|no_op): (?:unused [a-z_]+ must be null|[a-z_]+ is required)",
            detail,
        ):
            detail = item["type"]
        issues.append(f"{path}: {detail}")
    return "; ".join(issues)[:240]


class HumanOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=160)
    expected_revision: int | None = Field(default=None, ge=0)
    title: str | None = Field(default=None, max_length=120)
    topic_id: str | None = Field(default=None, max_length=160)
    kind: (
        Literal[
            "context", "point", "proposal", "concern", "decision", "question", "action"
        ]
        | None
    ) = None
    content_md: str | None = Field(default=None, max_length=20000)
    topic_ids: list[str] | None = Field(default=None, max_length=2000)
    action: Literal["accept", "dismiss"] | None = None
