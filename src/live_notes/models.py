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
    min_interval_sec=18,
    target_interval_sec=35,
    max_wait_sec=75,
    min_pending_chars=120,
    max_batch_chars=6000,
    max_operations_per_batch=20,
    max_calls_per_hour=90,
    invocation_timeout_sec=120,
    max_structural_repair_calls=1,
    consolidation_interval_sec=240,
    min_consolidation_batches=4,
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
    min_interval_sec: float = Field(default=18, ge=1, le=3600)
    target_interval_sec: float = Field(default=35, ge=1, le=3600)
    max_wait_sec: float = Field(default=75, ge=1, le=7200)
    min_pending_chars: int = Field(default=120, ge=1, le=6000)
    max_batch_chars: int = Field(default=6000, ge=120, le=6000)
    max_operations_per_batch: int = Field(default=20, ge=1, le=20)
    max_calls_per_hour: int = Field(default=90, ge=1, le=360)
    invocation_timeout_sec: float = Field(default=120, ge=1, le=300)
    max_structural_repair_calls: int = Field(default=1, ge=0, le=1)
    consolidation_interval_sec: int = Field(default=240, ge=1, le=3600)
    min_consolidation_batches: int = Field(default=4, ge=1, le=20)
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


SEMANTIC_TYPES = (
    "CREATE_TOPIC", "UPSERT_KNOWLEDGE", "RESOLVE_QUESTION", "REVISE_CLAIM",
    "MERGE_BLOCKS", "MOVE_BLOCK", "RESTRUCTURE_TOPIC", "UPDATE_TAKEAWAY",
    "SUGGEST_EDIT", "NO_CHANGE",
)
SEMANTIC_ROLES = (
    "explanation", "fact", "conclusion", "decision", "option", "reason",
    "risk", "open_question", "action", "code_path", "comparison", "note",
)
EPISTEMIC_STATES = ("tentative", "supported", "confirmed", "disputed", "superseded", "unknown")
QUESTION_STATES = ("open", "answered", "deferred", "not_applicable")


class SemanticOperation(BaseModel):
    """V2 proposal vocabulary. Applicability and evidence are checked transactionally."""
    model_config = ConfigDict(extra="forbid", strict=True)
    op_id: str = Field(min_length=1, max_length=160)
    type: Literal[
        "CREATE_TOPIC", "UPSERT_KNOWLEDGE", "RESOLVE_QUESTION", "REVISE_CLAIM",
        "MERGE_BLOCKS", "MOVE_BLOCK", "RESTRUCTURE_TOPIC", "UPDATE_TAKEAWAY",
        "SUGGEST_EDIT", "NO_CHANGE",
    ]
    topic_id: str | None = Field(default=None, max_length=160)
    title: str | None = Field(default=None, max_length=120)
    block_id: str | None = Field(default=None, max_length=160)
    question_id: str | None = Field(default=None, max_length=160)
    target_id: str | None = Field(default=None, max_length=160)
    semantic_role: Literal[
        "explanation", "fact", "conclusion", "decision", "option", "reason",
        "risk", "open_question", "action", "code_path", "comparison", "note",
    ] | None = None
    epistemic_state: Literal["tentative", "supported", "confirmed", "disputed", "superseded", "unknown"] | None = None
    resolution_state: Literal["open", "answered", "deferred", "not_applicable"] | None = None
    content_md: str | None = Field(default=None, max_length=20000)
    expected_revision: int | None = Field(default=None, ge=0)
    source_block_ids: list[str] = Field(default_factory=list, max_length=100)
    target_block_id: str | None = Field(default=None, max_length=160)
    expected_revisions: dict[str, int] = Field(default_factory=dict, max_length=100)
    expected_topic_revisions: dict[str, int] = Field(default_factory=dict, max_length=100)
    target_topic_id: str | None = Field(default=None, max_length=160)
    source_topic_ids: list[str] = Field(default_factory=list, max_length=100)
    block_order: list[str] = Field(default_factory=list, max_length=500)
    target_type: Literal["block", "topic", "document"] | None = None
    aliases: list[str] = Field(default_factory=list, max_length=100)
    canonical_terms: list[str] = Field(default_factory=list, max_length=100)
    confidence: float = Field(default=0.7, ge=0, le=1)
    verified_against_source: bool | None = None
    source_segments: list[int] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def fields_for_type(self):
        allowed = {
            "CREATE_TOPIC": {"topic_id", "title"},
            "UPSERT_KNOWLEDGE": {"topic_id", "block_id", "question_id", "semantic_role", "epistemic_state", "resolution_state", "content_md", "expected_revision", "aliases", "canonical_terms", "verified_against_source"},
        "RESOLVE_QUESTION": {"topic_id", "block_id", "question_id", "semantic_role", "epistemic_state", "content_md", "expected_revision", "expected_revisions", "aliases", "canonical_terms"},
            "REVISE_CLAIM": {"block_id", "semantic_role", "epistemic_state", "content_md", "expected_revision", "verified_against_source"},
            "MERGE_BLOCKS": {"source_block_ids", "target_block_id", "content_md", "expected_revisions", "aliases", "canonical_terms"},
            "MOVE_BLOCK": {"block_id", "expected_revision", "target_topic_id"},
            "RESTRUCTURE_TOPIC": {"topic_id", "title", "target_topic_id", "source_topic_ids", "block_order", "expected_topic_revisions"},
            "UPDATE_TAKEAWAY": {"content_md", "aliases", "canonical_terms"},
            "SUGGEST_EDIT": {"target_type", "target_id", "expected_revision", "semantic_role", "epistemic_state", "content_md", "aliases", "canonical_terms"},
            "NO_CHANGE": set(),
        }
        required = {
            "CREATE_TOPIC": {"title"},
            "UPSERT_KNOWLEDGE": {"topic_id", "semantic_role", "content_md"},
            "RESOLVE_QUESTION": {"question_id", "expected_revision", "content_md"},
            "REVISE_CLAIM": {"block_id", "expected_revision", "content_md"},
            "MERGE_BLOCKS": {"source_block_ids", "target_block_id", "content_md", "expected_revisions"},
            "MOVE_BLOCK": {"block_id", "expected_revision", "target_topic_id"},
            "RESTRUCTURE_TOPIC": {"topic_id", "expected_topic_revisions"},
            "UPDATE_TAKEAWAY": {"content_md"},
            "SUGGEST_EDIT": {"target_type", "target_id", "expected_revision", "content_md"},
            "NO_CHANGE": set(),
        }
        for name in set(type(self).model_fields) - {"op_id", "type", "confidence", "source_segments"}:
            value = getattr(self, name)
            if name in required[self.type] and (value is None or value == "" or value == [] or value == {}):
                raise ValueError(f"{self.type}: {name} is required")
            if name not in allowed[self.type] and value not in (None, [], {}):
                raise ValueError(f"{self.type}: unused {name} must be null/empty")
        if self.type == "UPSERT_KNOWLEDGE" and self.block_id and self.expected_revision is None:
            raise ValueError("UPSERT_KNOWLEDGE: expected_revision is required when updating")
        if self.type in ("REVISE_CLAIM", "MOVE_BLOCK") and self.expected_revision is None:
            raise ValueError(f"{self.type}: expected_revision is required")
        if self.type == "RESOLVE_QUESTION" and self.expected_revision is None:
            raise ValueError("RESOLVE_QUESTION: expected_revision is required")
        if self.type == "RESOLVE_QUESTION" and self.block_id and self.block_id in self.expected_revisions:
            if type(self.expected_revisions[self.block_id]) is not int:
                raise ValueError("RESOLVE_QUESTION: answer block revision must be an integer")
        if self.type == "SUGGEST_EDIT" and self.expected_revision is None:
            raise ValueError("SUGGEST_EDIT: expected_revision is required")
        if self.type == "RESTRUCTURE_TOPIC" and not (set(self.source_topic_ids) | {self.topic_id}) <= set(self.expected_topic_revisions):
            raise ValueError("RESTRUCTURE_TOPIC: expected_topic_revisions is required for every input")
        if self.type == "MERGE_BLOCKS":
            ids = set(self.source_block_ids) | {self.target_block_id}
            if not ids <= set(self.expected_revisions):
                raise ValueError("MERGE_BLOCKS: expected_revisions is required for every input")
        return self


class SemanticOperationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[2]
    batch_id: str = Field(min_length=1, max_length=160)
    base_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=500)
    operations: list[SemanticOperation] = Field(max_length=20)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int or value != 2:
            raise ValueError("schema_version must be the integer 2")
        return value


def semantic_operation_plan_schema() -> dict:
    """Strict model-facing v2 schema; each variant allows only its operation fields."""
    schema = SemanticOperationPlan.model_json_schema()
    schema["properties"]["schema_version"] = {"type": "integer", "const": 2}
    schema["required"] = ["schema_version", "batch_id", "base_revision", "reason", "operations"]
    base = schema["$defs"]["SemanticOperation"]["properties"]
    field_names = [name for name in base if name not in ("type", "op_id")]
    allowed = {
        "CREATE_TOPIC": {"topic_id", "title"},
        "UPSERT_KNOWLEDGE": {"topic_id", "block_id", "question_id", "semantic_role", "epistemic_state", "resolution_state", "content_md", "expected_revision", "aliases", "canonical_terms", "verified_against_source"},
        "RESOLVE_QUESTION": {"topic_id", "block_id", "question_id", "semantic_role", "epistemic_state", "content_md", "expected_revision", "expected_revisions", "aliases", "canonical_terms"},
        "REVISE_CLAIM": {"block_id", "semantic_role", "epistemic_state", "content_md", "expected_revision", "verified_against_source"},
        "MERGE_BLOCKS": {"source_block_ids", "target_block_id", "content_md", "expected_revisions", "aliases", "canonical_terms"},
        "MOVE_BLOCK": {"block_id", "expected_revision", "target_topic_id"},
        "RESTRUCTURE_TOPIC": {"topic_id", "title", "target_topic_id", "source_topic_ids", "block_order", "expected_topic_revisions"},
        "UPDATE_TAKEAWAY": {"content_md", "aliases", "canonical_terms"},
        "SUGGEST_EDIT": {"target_type", "target_id", "expected_revision", "semantic_role", "epistemic_state", "content_md", "aliases", "canonical_terms"},
        "NO_CHANGE": set(),
    }
    required = {
        "CREATE_TOPIC": {"title"}, "UPSERT_KNOWLEDGE": {"topic_id", "semantic_role", "content_md"},
        "RESOLVE_QUESTION": {"question_id", "expected_revision", "content_md"}, "REVISE_CLAIM": {"block_id", "expected_revision", "content_md"},
        "MERGE_BLOCKS": {"source_block_ids", "target_block_id", "content_md", "expected_revisions"},
        "MOVE_BLOCK": {"block_id", "expected_revision", "target_topic_id"}, "RESTRUCTURE_TOPIC": {"topic_id", "expected_topic_revisions"},
        "UPDATE_TAKEAWAY": {"content_md"}, "SUGGEST_EDIT": {"target_type", "target_id", "expected_revision", "content_md"},
        "NO_CHANGE": set(),
    }
    variants = []
    for kind in SEMANTIC_TYPES:
        props = {"op_id": base["op_id"], "type": {"type": "string", "const": kind}}
        for name in field_names:
            prop = deepcopy(base[name])
            if name not in allowed[kind]:
                if name == "source_segments":
                    props[name] = deepcopy(base[name])
                elif name in {"source_block_ids", "source_topic_ids", "block_order", "aliases", "canonical_terms"}:
                    props[name] = deepcopy(base[name])
                    props[name]["maxItems"] = 0
                elif name in {"expected_revisions", "expected_topic_revisions"}:
                    props[name] = deepcopy(base[name])
                    props[name]["maxProperties"] = 0
                elif name == "confidence":
                    props[name] = deepcopy(base[name])
                else:
                    props[name] = {"type": "null"}
            else:
                props[name] = prop
                if name in required[kind] and "anyOf" in prop:
                    prop["anyOf"] = [part for part in prop["anyOf"] if part.get("type") != "null"]
        variant = {"type": "object", "properties": props, "required": ["op_id", "type", *field_names], "additionalProperties": False}
        if kind == "UPSERT_KNOWLEDGE":
            create_variant = deepcopy(variant)
            create_variant["properties"]["block_id"] = {"type":"null"}
            create_variant["properties"]["expected_revision"] = {"type":"null"}
            update_variant = deepcopy(variant)
            update_variant["properties"]["block_id"] = {"type":"string","minLength":1,"maxLength":160}
            update_variant["properties"]["expected_revision"] = {"type":"integer","minimum":0}
            variants.extend([create_variant, update_variant])
        else:
            variants.append(variant)
        if kind == "RESTRUCTURE_TOPIC":
            variants[-1]["properties"]["expected_topic_revisions"]["minProperties"] = 1
    schema["$defs"]["SemanticOperation"] = {"anyOf": variants}
    return schema


def legacy_operation_plan_schema() -> dict:
    """v1 schema retained for compatibility and replay tests."""
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
                prop["anyOf"] = [part for part in prop["anyOf"] if part.get("type") != "null"]
        variants.append(variant)
    schema["$defs"]["Operation"] = {"anyOf": variants}
    return schema


def operation_plan_schema() -> dict:
    """Legacy v1 name retained; live CLI calls use semantic_operation_plan_schema."""
    return legacy_operation_plan_schema()


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


class DocumentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    op: Literal["update_topic", "update_block", "create_topic", "create_block", "delete_topic", "delete_block", "reorder_topics"]
    id: str | None = Field(default=None, max_length=160)
    client_id: str | None = Field(default=None, max_length=160)
    expected_revision: int | None = Field(default=None, ge=0)
    topic_id: str | None = Field(default=None, max_length=160)
    client_topic_id: str | None = Field(default=None, max_length=160)
    title: str | None = Field(default=None, max_length=120)
    markdown: str | None = Field(default=None, max_length=20000)
    kind: Literal["context", "point", "proposal", "concern", "decision", "question", "action"] | None = None
    position: int | None = Field(default=None, ge=0)
    ids: list[str] | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_operation(self):
        required = {
            "update_topic": ("id", "expected_revision", "title"),
            "update_block": ("id", "expected_revision", "markdown"),
            "create_topic": ("client_id", "title", "position"),
            "create_block": ("client_id", "markdown", "kind", "position"),
            "delete_topic": ("id", "expected_revision"),
            "delete_block": ("id", "expected_revision"),
            "reorder_topics": ("ids",),
        }[self.op]
        for name in required:
            value = getattr(self, name)
            if value is None or value == "":
                raise ValueError(f"{self.op}: {name} is required")
        if self.op == "create_block" and (self.topic_id is None) == (self.client_topic_id is None):
            raise ValueError("create_block: specify exactly one of topic_id or client_topic_id")
        allowed = {
            "update_topic": {"id", "expected_revision", "title"},
            "update_block": {"id", "expected_revision", "markdown", "kind"},
            "create_topic": {"client_id", "title", "position"},
            "create_block": {"client_id", "topic_id", "client_topic_id", "markdown", "kind", "position"},
            "delete_topic": {"id", "expected_revision"},
            "delete_block": {"id", "expected_revision"},
            "reorder_topics": {"ids"},
        }[self.op] | {"op"}
        for name in set(type(self).model_fields) - allowed:
            value = getattr(self, name)
            if value not in (None, [], {}):
                raise ValueError(f"{self.op}: unused {name} is forbidden")
        return self


class DocumentEditsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_note_revision: int = Field(ge=0)
    request_id: str = Field(min_length=1, max_length=160)
    operations: list[DocumentEdit] = Field(min_length=1, max_length=200)
