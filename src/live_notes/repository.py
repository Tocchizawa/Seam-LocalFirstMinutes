from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .models import DocumentEditsRequest, HumanOperation, OperationPlan, SemanticOperation, SemanticOperationPlan
from .renderer import render_markdown


def now():
    return datetime.now(UTC).isoformat()


def uid():
    return uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_note_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO live_note_schema VALUES(1);
CREATE TABLE IF NOT EXISTS live_note_sessions(
 session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, minutes_id TEXT UNIQUE,
 provider TEXT NOT NULL, status TEXT NOT NULL, note_revision INTEGER NOT NULL DEFAULT 0,
 last_processed_segment_seq INTEGER NOT NULL DEFAULT 0, active_topic_id TEXT,
 last_error TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
 provider_config_json TEXT NOT NULL DEFAULT '{}', live_config_json TEXT NOT NULL DEFAULT '{}',
 meeting_meta_json TEXT NOT NULL DEFAULT '{}',
 paused INTEGER NOT NULL DEFAULT 0, stopped INTEGER NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
 flush_requested INTEGER NOT NULL DEFAULT 0, finalize_pass_done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS live_note_segments(
 session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 segment_seq INTEGER NOT NULL, source_id TEXT NOT NULL, start_ms INTEGER NOT NULL,
 end_ms INTEGER NOT NULL, speaker_label TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(session_id,segment_seq), UNIQUE(session_id,source_id));
CREATE TABLE IF NOT EXISTS live_note_topics(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 title TEXT NOT NULL, title_owner TEXT NOT NULL CHECK(title_owner IN ('human','ai')),
 position INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0, deleted_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 archived_at TEXT, digest TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_live_topics ON live_note_topics(session_id,position);
CREATE TABLE IF NOT EXISTS live_note_blocks(
 id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES live_note_topics(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('context','point','proposal','concern','decision','question','action')),
 content_md TEXT NOT NULL, owner TEXT NOT NULL CHECK(owner IN ('human','ai')),
 position INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 source_segments_json TEXT NOT NULL DEFAULT '[]', deleted_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 semantic_role TEXT NOT NULL DEFAULT 'note', epistemic_state TEXT NOT NULL DEFAULT 'unknown',
 resolution_state TEXT, user_touched INTEGER NOT NULL DEFAULT 0, protected INTEGER NOT NULL DEFAULT 0,
 verified_against_source INTEGER NOT NULL DEFAULT 0, aliases_json TEXT NOT NULL DEFAULT '[]',
 canonical_terms_json TEXT NOT NULL DEFAULT '[]', superseded_by TEXT, semantic_v2 INTEGER NOT NULL DEFAULT 0,
 is_takeaway INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_live_blocks ON live_note_blocks(topic_id,position);
CREATE TABLE IF NOT EXISTS live_note_batches(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 seq_from INTEGER NOT NULL, seq_to INTEGER NOT NULL, status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, repair_calls INTEGER NOT NULL DEFAULT 0,
 provider TEXT NOT NULL, last_error TEXT, context_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT 'fast',
 UNIQUE(session_id,seq_from,seq_to));
CREATE INDEX IF NOT EXISTS idx_live_batches ON live_note_batches(session_id,status,seq_from);
CREATE TABLE IF NOT EXISTS live_note_events(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 note_revision INTEGER NOT NULL, actor TEXT NOT NULL, operation_json TEXT NOT NULL,
 undo_json TEXT, created_at TEXT NOT NULL, UNIQUE(session_id,note_revision));
CREATE TABLE IF NOT EXISTS live_note_suggestions(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 target_type TEXT NOT NULL, target_id TEXT, suggested_operation_json TEXT NOT NULL,
 reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_note_applied_operations(
 op_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES live_note_batches(id) ON DELETE CASCADE,
 outcome TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_note_requests(
 session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 request_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(session_id,request_id));
CREATE TABLE IF NOT EXISTS live_note_calls(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 batch_id TEXT NOT NULL, started_at REAL NOT NULL, duration_ms REAL, error TEXT);
CREATE INDEX IF NOT EXISTS idx_live_calls ON live_note_calls(started_at);
CREATE TABLE IF NOT EXISTS live_note_questions(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 topic_id TEXT NOT NULL REFERENCES live_note_topics(id), text TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','answered','deferred','not_applicable')),
 answer_node_id TEXT, resolution_evidence_json TEXT NOT NULL DEFAULT '[]',
 question_node_id TEXT,
 revision INTEGER NOT NULL DEFAULT 0, created_seq INTEGER NOT NULL DEFAULT 0,
 resolved_seq INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_live_questions ON live_note_questions(session_id,state,created_seq);
"""


def migrate(conn):
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM live_note_schema WHERE version=1")
    conn.execute("INSERT OR IGNORE INTO live_note_schema(version) VALUES(2)")
    additions = {
        "live_note_topics": {"archived_at": "TEXT", "digest": "TEXT NOT NULL DEFAULT ''"},
        "live_note_blocks": {
            "semantic_role": "TEXT NOT NULL DEFAULT 'note'", "epistemic_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "resolution_state": "TEXT", "user_touched": "INTEGER NOT NULL DEFAULT 0",
            "protected": "INTEGER NOT NULL DEFAULT 0", "verified_against_source": "INTEGER NOT NULL DEFAULT 0",
            "aliases_json": "TEXT NOT NULL DEFAULT '[]'", "canonical_terms_json": "TEXT NOT NULL DEFAULT '[]'",
            "superseded_by": "TEXT", "semantic_v2": "INTEGER NOT NULL DEFAULT 0",
            "is_takeaway": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, columns in additions.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, declaration in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    if "meeting_meta_json" not in {
        row[1] for row in conn.execute("PRAGMA table_info(live_note_sessions)")
    }:
        conn.execute(
            "ALTER TABLE live_note_sessions ADD COLUMN meeting_meta_json TEXT NOT NULL DEFAULT '{}'"
        )
    for table, column, declaration in (
        ("live_note_sessions", "finalize_pass_done", "INTEGER NOT NULL DEFAULT 0"),
        ("live_note_batches", "kind", "TEXT NOT NULL DEFAULT 'fast'"),
        ("live_note_questions", "question_node_id", "TEXT"),
    ):
        if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    # Older resolvers could queue a RESOLVE_QUESTION against a question that
    # was already answered. Those rows cannot ever pass acceptance checks.
    # Keep them in history and remove them from the actionable pending queue.
    for suggestion in conn.execute(
        "SELECT id,session_id,suggested_operation_json FROM live_note_suggestions WHERE status='pending'"
    ).fetchall():
        try:
            operation = json.loads(suggestion["suggested_operation_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if operation.get("type") != "RESOLVE_QUESTION":
            continue
        question = conn.execute(
            "SELECT state FROM live_note_questions WHERE id=? AND session_id=?",
            (operation.get("question_id"), suggestion["session_id"]),
        ).fetchone()
        if question and question["state"] != "open":
            conn.execute("UPDATE live_note_suggestions SET status='superseded' WHERE id=?", (suggestion["id"],))
    # Existing minutes APIs remain compatible, including project move and deletion.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='minutes'").fetchone():
        conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS minutes_live_delete AFTER DELETE ON minutes BEGIN
          DELETE FROM live_note_sessions WHERE session_id=old.session_id;
        END;
        CREATE TRIGGER IF NOT EXISTS minutes_live_project AFTER UPDATE OF project_id ON minutes BEGIN
          UPDATE live_note_sessions SET project_id=new.project_id WHERE session_id=new.session_id;
        END;
        """)
    conn.commit()


class NoteError(Exception):
    def __init__(self, code, message, status=422, latest=None):
        super().__init__(message)
        self.code, self.status, self.latest = code, status, latest


class LiveNoteStore:
    """Synchronous operations. Create/use/close on the coordinator's single DB thread."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        migrate(self.conn)

    def close(self):
        self.conn.close()

    def session(self, sid):
        row = self.conn.execute(
            "SELECT * FROM live_note_sessions WHERE session_id=?", (sid,)
        ).fetchone()
        if row is None:
            raise NoteError("LIVE_NOTE_NOT_FOUND", "議論ノートがありません", 404)
        return dict(row)

    def create(self, sid, pid, provider, provider_config, settings):
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO live_note_sessions
              (session_id,project_id,provider,status,started_at,updated_at,provider_config_json,live_config_json)
              VALUES(?,?,?,'recording',?,?,?,?)""",
                (
                    sid,
                    pid,
                    provider,
                    now(),
                    now(),
                    json.dumps(provider_config),
                    json.dumps(settings),
                ),
            )
        return self.snapshot(sid)

    def set_metadata(self, sid, metadata):
        with self.conn:
            self.conn.execute(
                "UPDATE live_note_sessions SET meeting_meta_json=? WHERE session_id=?",
                (json.dumps(metadata, ensure_ascii=False), sid),
            )

    def sessions(self):
        return [
            dict(r)
            for r in self.conn.execute("""SELECT session_id,project_id,minutes_id,status,
          started_at,last_error,note_revision FROM live_note_sessions ORDER BY started_at DESC""")
        ]

    def snapshot(self, sid, include_deleted=False):
        s = self.session(sid)
        s["finalize_pass_done"] = bool(s.get("finalize_pass_done"))
        for key in ("provider_config_json", "live_config_json", "meeting_meta_json"):
            s.pop(key, None)
        s["topics"] = []
        suffix = "" if include_deleted else " AND deleted_at IS NULL"
        for r in self.conn.execute(
            "SELECT * FROM live_note_topics WHERE session_id=?"
            + suffix
            + ("" if include_deleted else " AND archived_at IS NULL")
            + " ORDER BY position,id",
            (sid,),
        ):
            topic = dict(r)
            topic["blocks"] = []
            for b in self.conn.execute(
                "SELECT * FROM live_note_blocks WHERE topic_id=?"
                + suffix
                + " ORDER BY position,id",
                (r["id"],),
            ):
                block = dict(b)
                block["source_segment_seqs"] = json.loads(
                    block.pop("source_segments_json")
                )
                block["aliases"] = json.loads(block.pop("aliases_json", "[]"))
                block["canonical_terms"] = json.loads(block.pop("canonical_terms_json", "[]"))
                block["user_touched"] = bool(block.get("user_touched"))
                block["protected"] = bool(block.get("protected"))
                block["verified_against_source"] = bool(block.get("verified_against_source"))
                topic["blocks"].append(block)
            s["topics"].append(topic)
        s["questions"] = [dict(r) for r in self.conn.execute(
            "SELECT * FROM live_note_questions WHERE session_id=? ORDER BY created_seq,id", (sid,)
        )]
        for question in s["questions"]:
            question["resolution_evidence"] = json.loads(question.pop("resolution_evidence_json"))
        metadata = json.loads(self.session(sid)["meeting_meta_json"])
        s["legacy_takeaway"] = metadata.get("takeaway")
        takeaway = self.conn.execute(
            "SELECT b.content_md FROM live_note_blocks b JOIN live_note_topics t ON t.id=b.topic_id "
            "WHERE t.session_id=? AND t.deleted_at IS NULL AND t.archived_at IS NULL "
            "AND b.deleted_at IS NULL AND b.is_takeaway=1 ORDER BY b.updated_at DESC LIMIT 1", (sid,)
        ).fetchone()
        # Read legacy metadata during migration, but new v2 takeaways live in the
        # ordinary editable topic/block stream.
        s["takeaway"] = takeaway[0] if takeaway else metadata.get("takeaway")
        # A stable, unified-editor projection. IDs remain the canonical DB IDs.
        nodes = []
        ordinal = 0
        for topic in s["topics"]:
            nodes.append({"id": topic["id"], "type": "topic", "topic_id": topic["id"],
                          "markdown": "## " + topic["title"], "position": ordinal, "section_position": topic["position"],
                          "revision": topic["revision"], "user_touched": topic["title_owner"] == "human",
                          "protected": topic["title_owner"] == "human", "attrs": {"title": topic["title"]}})
            ordinal += 1
            for block in topic["blocks"]:
                nodes.append({"id": block["id"], "type": "block", "topic_id": topic["id"],
                              "markdown": block["content_md"], "position": ordinal, "section_position": block["position"],
                              "revision": block["revision"], "semantic_role": block["semantic_role"],
                              "epistemic_state": block["epistemic_state"],
                              "resolution_state": block["resolution_state"],
                              "user_touched": block["user_touched"], "protected": block["protected"],
                              "verified_against_source": block["verified_against_source"],
                              "source_segments": block["source_segment_seqs"],
                              "semantic_v2": bool(block["semantic_v2"]),
                              "is_takeaway": bool(block.get("is_takeaway")),
                              "attrs": {"kind": block["kind"], "owner": block["owner"],
                                        "is_takeaway": bool(block.get("is_takeaway"))}})
                ordinal += 1
        s["document"] = {"id": sid, "title": metadata.get("title", ""),
                          "markdown": render_markdown(s["topics"]), "revision": s["note_revision"], "nodes": nodes}
        s["document_revision"] = s["note_revision"]
        s["suggestions"] = [
            self._present_suggestion(dict(r), s)
            for r in self.conn.execute(
                "SELECT * FROM live_note_suggestions WHERE session_id=? AND status='pending' ORDER BY created_at",
                (sid,),
            )
        ]
        s["pending_segments"] = self.conn.execute(
            "SELECT count(*) FROM live_note_segments WHERE session_id=? AND segment_seq>?",
            (sid, s["last_processed_segment_seq"]),
        ).fetchone()[0]
        s["processing"] = bool(
            self.conn.execute(
                "SELECT 1 FROM live_note_batches WHERE session_id=? AND status='processing'",
                (sid,),
            ).fetchone()
        )
        s["llm_calls"] = self.conn.execute(
            "SELECT count(*) FROM live_note_calls WHERE session_id=?", (sid,)
        ).fetchone()[0]
        first = self.conn.execute(
            "SELECT created_at FROM live_note_segments WHERE session_id=? AND segment_seq>? ORDER BY segment_seq LIMIT 1",
            (sid, s["last_processed_segment_seq"]),
        ).fetchone()
        s["queue_age_sec"] = (
            max(0, time.time() - datetime.fromisoformat(first[0]).timestamp())
            if first
            else 0
        )
        s["model"] = json.loads(self.session(sid)["provider_config_json"]).get(
            "model", ""
        )
        durations = [
            row[0]
            for row in self.conn.execute(
                "SELECT duration_ms FROM live_note_calls WHERE session_id=? AND duration_ms IS NOT NULL ORDER BY duration_ms",
                (sid,),
            )
        ]
        s["metrics"] = {
            "llm_calls": s["llm_calls"],
            "pending_segments": s["pending_segments"],
            "segments_processed": s["last_processed_segment_seq"],
            "llm_errors": self.conn.execute(
                "SELECT count(*) FROM live_note_calls WHERE session_id=? AND error IS NOT NULL",
                (sid,),
            ).fetchone()[0],
            "llm_duration_p50_ms": durations[int((len(durations) - 1) * 0.50)]
            if durations
            else None,
            "llm_duration_p95_ms": durations[int((len(durations) - 1) * 0.95)]
            if durations
            else None,
            "conflicts": len(s["suggestions"]),
        }

        return s

    def state(self, sid, **values):
        allowed = {
            "status",
            "last_error",
            "paused",
            "stopped",
            "failures",
            "next_attempt_at",
            "flush_requested",
        }
        if not values.keys() <= allowed:
            raise ValueError("invalid state columns")
        with self.conn:
            self.conn.execute(
                "UPDATE live_note_sessions SET "
                + ",".join(k + "=?" for k in values)
                + ",updated_at=? WHERE session_id=?",
                (*values.values(), now(), sid),
            )
        return self.snapshot(sid)

    def ingest(self, sid, segments):
        s = self.session(sid)
        limit = json.loads(s["live_config_json"]).get("max_batch_chars", 6000)
        with self.conn:
            seq = self.conn.execute(
                "SELECT coalesce(max(segment_seq),0) FROM live_note_segments WHERE session_id=?",
                (sid,),
            ).fetchone()[0]
            for seg in segments:
                text = seg.get("text", "").strip()
                for part, offset in enumerate(range(0, len(text), limit)):
                    source = f"{seg['segment_id']}:{part}"
                    if self.conn.execute(
                        "SELECT 1 FROM live_note_segments WHERE session_id=? AND source_id=?",
                        (sid, source),
                    ).fetchone():
                        continue
                    seq += 1
                    self.conn.execute(
                        "INSERT INTO live_note_segments VALUES(?,?,?,?,?,?,?,?)",
                        (
                            sid,
                            seq,
                            source,
                            round(float(seg.get("start", 0)) * 1000),
                            round(float(seg.get("end", 0)) * 1000),
                            seg.get("speaker_label"),
                            text[offset : offset + limit],
                            now(),
                        ),
                    )
        return self.snapshot(sid)

    def markdown(self, sid):
        snapshot = self.snapshot(sid)
        return render_markdown(snapshot["topics"])

    def _sync(self, sid):
        s = self.session(sid)
        if s["minutes_id"]:
            self.conn.execute(
                "UPDATE minutes SET summary=?,updated_at=? WHERE id=?",
                (self.markdown(sid), now(), s["minutes_id"]),
            )

    def link(self, sid, mid):
        with self.conn:
            row = self.conn.execute(
                "SELECT session_id FROM minutes WHERE id=?", (mid,)
            ).fetchone()
            if not row or row[0] != sid:
                raise NoteError("SESSION_MISMATCH", "会議の保存先が一致しません")
            self.conn.execute(
                "UPDATE live_note_sessions SET minutes_id=?,stopped=1,flush_requested=1,updated_at=? WHERE session_id=?",
                (mid, now(), sid),
            )
            self._sync(sid)
        return self.snapshot(sid)

    def recover_link(self, sid):
        if self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='minutes'"
        ).fetchone():
            row = self.conn.execute(
                "SELECT id FROM minutes WHERE session_id=?", (sid,)
            ).fetchone()
            if row:
                self.link(sid, row["id"])

    def _topic(self, sid, tid, deleted=False):
        row = self.conn.execute(
            "SELECT * FROM live_note_topics WHERE id=? AND session_id=?"
            + ("" if deleted else " AND deleted_at IS NULL"),
            (tid, sid),
        ).fetchone()
        if row is None:
            raise NoteError("TOPIC_NOT_FOUND", "論点がありません", 404)
        return dict(row)

    def _block(self, sid, bid, deleted=False):
        row = self.conn.execute(
            """SELECT b.* FROM live_note_blocks b JOIN live_note_topics t ON t.id=b.topic_id
          WHERE b.id=? AND t.session_id=?"""
            + ("" if deleted else " AND b.deleted_at IS NULL AND t.deleted_at IS NULL"),
            (bid, sid),
        ).fetchone()
        if row is None:
            raise NoteError("BLOCK_NOT_FOUND", "項目がありません", 404)
        return dict(row)

    def _revision(self, row, expected):
        if expected is None:
            raise NoteError("REVISION_REQUIRED", "expected_revisionが必要です")
        if row["revision"] != expected:
            raise NoteError(
                "REVISION_CONFLICT",
                "新しい変更があります。入力を保持して再確認してください",
                409,
                row,
            )

    def _event(self, sid, actor, operations, before=None):
        self.conn.execute(
            "UPDATE live_note_sessions SET note_revision=note_revision+1,updated_at=? WHERE session_id=?",
            (now(), sid),
        )
        revision = self.session(sid)["note_revision"]
        self.conn.execute(
            "INSERT INTO live_note_events VALUES(?,?,?,?,?,?,?)",
            (
                uid(),
                sid,
                revision,
                actor,
                json.dumps(operations, ensure_ascii=False),
                json.dumps(before, ensure_ascii=False) if before else None,
                now(),
            ),
        )
        self._sync(sid)
        return revision

    @staticmethod
    def _union(*values):
        result = []
        for value in values:
            for item in value or []:
                if item not in result:
                    result.append(item)
        return result

    def _semantic_suggestion(self, sid, op, reason, target_type="block", target_id=None):
        operation = op.model_dump() if hasattr(op, "model_dump") else dict(op)
        intent = operation.get("type") or operation.get("action")
        proposed = operation.get("content_md") or operation.get("title") or operation.get("markdown") or ""
        normalized = re.sub(r"\s+", " ", proposed).strip().casefold()
        for old in self.conn.execute(
            "SELECT * FROM live_note_suggestions WHERE session_id=? AND status='pending' AND target_type=? AND target_id=?",
            (sid, target_type, target_id),
        ).fetchall():
            previous = json.loads(old["suggested_operation_json"])
            if (previous.get("type") or previous.get("action")) != intent:
                continue
            old_text = previous.get("content_md") or previous.get("title") or previous.get("markdown") or ""
            if re.sub(r"\s+", " ", old_text).strip().casefold() == normalized:
                merged_sources = self._union(previous.get("source_segments", []), operation.get("source_segments", []))
                if merged_sources != previous.get("source_segments", []):
                    previous["source_segments"] = merged_sources
                    self.conn.execute("UPDATE live_note_suggestions SET suggested_operation_json=? WHERE id=?", (json.dumps(previous, ensure_ascii=False), old["id"]))
                return {"id": old["id"], "reason": old["reason"], "target_id": target_id, "duplicate": True}
            self.conn.execute("UPDATE live_note_suggestions SET status='superseded' WHERE id=?", (old["id"],))
        suggestion_id = uid()
        self.conn.execute(
            "INSERT INTO live_note_suggestions VALUES(?,?,?,?,?,?,?,?)",
            (suggestion_id, sid, target_type, target_id,
             json.dumps(operation, ensure_ascii=False), reason, "pending", now()),
        )
        return {"id": suggestion_id, "reason": reason, "target_id": target_id}

    def _present_suggestion(self, row, snapshot):
        """Add a readable, evidence-linked projection while preserving legacy fields."""
        op = json.loads(row["suggested_operation_json"])
        target_id = row.get("target_id")
        target_type = row.get("target_type")
        target_title, current_md = "", ""
        if target_type == "topic":
            target = next((t for t in snapshot["topics"] if t["id"] == target_id), None)
            if target:
                target_title = target["title"]
                current_md = target["title"] if op.get("title") else render_markdown([target])
        else:
            for topic in snapshot["topics"]:
                if target_type == "block":
                    block = next((b for b in topic["blocks"] if b["id"] == target_id), None)
                    if block:
                        target_title, current_md = topic["title"], block["content_md"]
                        break
                elif target_type == "topic" and topic["id"] == target_id:
                    target_title = topic["title"]
                    current_md = topic["title"] if op.get("title") else render_markdown([topic])
        source_segments = []
        for seq in op.get("source_segments", []):
            segment = self.conn.execute(
                "SELECT segment_seq,start_ms,text FROM live_note_segments WHERE session_id=? AND segment_seq=?",
                (row["session_id"], seq),
            ).fetchone()
            if segment:
                source_segments.append({"seq": segment["segment_seq"], "start_sec": segment["start_ms"] / 1000, "text": segment["text"]})
        reason = row.get("reason", "")
        if "人間" in reason:
            reason_code = "user_edit"
        elif "AI" in reason and "更新" in reason:
            reason_code = "newer_ai_update"
        elif any(word in reason for word in ("質問", "回答済み", "保留中")):
            reason_code = "closed_question"
        else:
            reason_code = "closed_question" if op.get("type") == "RESOLVE_QUESTION" else "newer_ai_update" if "版競合" in reason else "user_edit"
        if op.get("title"):
            summary = f"「{target_title or '論点'}」の見出しを変更する提案"
        elif "回答" in reason or "質問" in reason:
            summary = f"「{target_title or '論点'}」の回答候補を確認"
        elif target_type == "block":
            summary = f"「{target_title or '論点'}」の記述を更新する提案"
        elif target_type == "topic":
            summary = f"「{target_title or '論点'}」への知見追加を提案"
        else:
            summary = "提案内容を確認"
        row["presentation"] = {
            "summary": summary,
            "reason_code": reason_code,
            "target_title": target_title,
            "current_md": current_md,
            "proposed_md": op.get("content_md") or op.get("markdown") or op.get("title") or "",
            "source_segments": source_segments,
        }
        return row

    def _apply_v2(self, sid, batch, context, raw):
        plan = SemanticOperationPlan.model_validate(raw)
        if plan.batch_id != batch["id"] or plan.base_revision != context["base_note_revision"]:
            raise NoteError("INVALID_PLAN", "バッチまたは基準revisionが不一致です")
        settings = json.loads(self.session(sid)["live_config_json"])
        if len(plan.operations) > settings["max_operations_per_batch"]:
            raise NoteError("INVALID_PLAN", "操作数が上限を超えました")
        valid_sources = {r["segment_seq"]: r["text"] for r in context["segments"]}
        applied, conflicts, seen = [], [], set()
        temp_topics = {}
        with self.conn:
            current = self.conn.execute("SELECT * FROM live_note_batches WHERE id=? AND session_id=?", (batch["id"], sid)).fetchone()
            if current is None:
                raise NoteError("BATCH_NOT_FOUND", "会議に属するバッチがありません", 404)
            if current["status"] == "done":
                return self.snapshot(sid), []
            cursor = self.session(sid)["last_processed_segment_seq"]
            special_pass = batch.get("kind", "fast") in ("consolidate", "finalize") and batch["seq_from"] > batch["seq_to"]
            if batch["seq_from"] != current["seq_from"] or batch["seq_to"] != current["seq_to"] or batch["seq_from"] != cursor + 1 or (not special_pass and batch["seq_to"] < batch["seq_from"]):
                raise NoteError("BATCH_RANGE_CONFLICT", "バッチの発話範囲が不一致です", 409)
            before = self.snapshot(sid, True)
            # Resolve plan-local topic references before applying operations so a
            # valid UPSERT may refer to new:1 even if CREATE_TOPIC appears later.
            created_topics = set()
            for topic_op in plan.operations:
                if topic_op.type != "CREATE_TOPIC" or not topic_op.topic_id:
                    continue
                if not re.fullmatch(r"new:\d+", topic_op.topic_id) or topic_op.topic_id in temp_topics:
                    raise NoteError("INVALID_PLAN", "仮topic IDが不正です")
                norm = lambda value: re.sub(r"\s+", "", value).casefold()
                existing = next((t for t in self.snapshot(sid)["topics"] if norm(t["title"]) == norm(topic_op.title)), None)
                tid = existing["id"] if existing else self._new_topic(sid, topic_op.title, "ai")
                if not existing:
                    created_topics.add(tid)
                temp_topics[topic_op.topic_id] = tid
            for op in plan.operations:
                if not op.op_id.startswith(batch["id"] + ":") or op.op_id in seen:
                    raise NoteError("INVALID_PLAN", "操作IDが不正です")
                seen.add(op.op_id)
                if any(type(seq) is not int or seq not in valid_sources for seq in op.source_segments):
                    raise NoteError("INVALID_EVIDENCE", "存在しない発話が参照されました")
                if op.verified_against_source is True:
                    raise NoteError("INVALID_EVIDENCE", "会議の発話だけでは外部ソースの検証を証明できません")
                if op.type != "NO_CHANGE" and not op.source_segments and batch.get("kind", "fast") == "fast":
                    raise NoteError("INVALID_EVIDENCE", "発話の根拠が必要です")
                if self.conn.execute("SELECT 1 FROM live_note_applied_operations WHERE op_id=?", (op.op_id,)).fetchone():
                    raise NoteError("INVALID_PLAN", "別のバッチの操作IDです")
                conflict = None
                outcome = op.model_dump()
                if op.type == "NO_CHANGE":
                    pass
                elif op.type == "CREATE_TOPIC":
                    if not op.title or not op.title.strip():
                        raise NoteError("INVALID_PLAN", "見出しが必要です")
                    norm = lambda value: re.sub(r"\s+", "", value).casefold()
                    existing = next((t for t in self.snapshot(sid)["topics"] if norm(t["title"]) == norm(op.title)), None)
                    tid = temp_topics.get(op.topic_id) if op.topic_id else (existing["id"] if existing else self._new_topic(sid, op.title, "ai"))
                    if op.topic_id:
                        if temp_topics.get(op.topic_id) != tid:
                            raise NoteError("INVALID_PLAN", "仮topic IDの参照が一致しません")
                    outcome.update({"topic_id": tid, "created": tid in created_topics})
                elif op.type == "UPSERT_KNOWLEDGE":
                    if not op.topic_id or not op.content_md or not op.content_md.strip() or not op.semantic_role:
                        raise NoteError("INVALID_PLAN", "知見にはtopic、semantic_role、本文が必要です")
                    topic = self._topic(sid, temp_topics.get(op.topic_id, op.topic_id))
                    evidence = " ".join(valid_sources[x] for x in op.source_segments)
                    if op.semantic_role == "decision" and not re.search(r"決定|合意|承認|確定|採用します|で進めます|agree(?:d)?|decid(?:e|ed)", evidence, re.I):
                        conflict = "明示的な決定根拠がないため保留"
                    elif op.block_id:
                        block = self._block(sid, op.block_id)
                        if block["topic_id"] != topic["id"]:
                            raise NoteError("INVALID_PLAN", "対象項目が別のtopicにあります")
                        if block["owner"] == "human" or block["protected"]:
                            conflict = "人間が編集した内容を保護しました"
                        elif op.expected_revision is not None and block["revision"] != op.expected_revision:
                            conflict = "AIノートが更新されたため新しい根拠を保留しました"
                        else:
                            old_src = json.loads(block["source_segments_json"])
                            self.conn.execute("UPDATE live_note_blocks SET content_md=?,semantic_role=?,epistemic_state=?,resolution_state=?,verified_against_source=?,source_segments_json=?,aliases_json=?,canonical_terms_json=?,semantic_v2=1,revision=revision+1,updated_at=? WHERE id=?",
                            (op.content_md, op.semantic_role, op.epistemic_state or "supported", op.resolution_state, int(bool(op.verified_against_source)), json.dumps(self._union(old_src, op.source_segments)), json.dumps(self._union(json.loads(block["aliases_json"]), op.aliases)), json.dumps(self._union(json.loads(block["canonical_terms_json"]), op.canonical_terms)), now(), block["id"]))
                            outcome["block_id"] = block["id"]
                    else:
                        legacy_kind = {"open_question":"question", "decision":"decision", "action":"action", "risk":"concern", "option":"proposal"}.get(op.semantic_role, "point")
                        bid = self._new_block(topic["id"], legacy_kind, op.content_md, "ai", op.source_segments, semantic_v2=True)
                        self.conn.execute("UPDATE live_note_blocks SET semantic_role=?,epistemic_state=?,resolution_state=?,verified_against_source=?,aliases_json=?,canonical_terms_json=? WHERE id=?",
                            (op.semantic_role, op.epistemic_state or "supported", op.resolution_state, int(bool(op.verified_against_source)), json.dumps(op.aliases), json.dumps(op.canonical_terms), bid))
                        outcome["block_id"] = bid
                        if op.semantic_role == "open_question":
                            qid = op.question_id or uid()
                            prior_question = self.conn.execute("SELECT session_id,topic_id FROM live_note_questions WHERE id=?",(qid,)).fetchone()
                            if prior_question and (prior_question["session_id"] != sid or prior_question["topic_id"] != topic["id"]):
                                raise NoteError("INVALID_PLAN", "質問IDが別の会議またはtopicに属しています")
                            self.conn.execute("INSERT INTO live_note_questions(id,session_id,topic_id,text,state,answer_node_id,resolution_evidence_json,question_node_id,revision,created_seq,created_at,updated_at) VALUES(?,?,?,?, 'open',NULL,?,?,0,?,?,?) ON CONFLICT(id) DO UPDATE SET text=excluded.text,question_node_id=excluded.question_node_id,revision=live_note_questions.revision+1,updated_at=excluded.updated_at",
                                (qid, sid, topic["id"], op.content_md, json.dumps(op.source_segments), bid, min(op.source_segments), now(), now()))
                            self.conn.execute("UPDATE live_note_blocks SET resolution_state='open' WHERE id=?", (bid,))
                            outcome["question_id"] = qid
                elif op.type == "RESOLVE_QUESTION":
                    q = self.conn.execute("SELECT * FROM live_note_questions WHERE id=? AND session_id=?", (op.question_id, sid)).fetchone()
                    if not q:
                        raise NoteError("QUESTION_NOT_FOUND", "質問がありません", 404)
                    if q["state"] != "open":
                        # Replayed resolutions must not create a dead-end suggestion against a
                        # closed question. Keep duplicate wording idempotent; retain a materially
                        # new answer as a revision-checked proposal on the existing answer.
                        if q["state"] == "answered" and q["answer_node_id"]:
                            answer = self._block(sid, q["answer_node_id"])
                            prior_evidence = json.loads(q["resolution_evidence_json"])
                            combined = self._union(prior_evidence, op.source_segments)
                            same_text = re.sub(r"\s+", " ", answer["content_md"]).strip().casefold() == re.sub(r"\s+", " ", (op.content_md or "")).strip().casefold()
                            if same_text:
                                if combined != prior_evidence:
                                    self.conn.execute("UPDATE live_note_blocks SET source_segments_json=?,revision=revision+1,updated_at=? WHERE id=?", (json.dumps(self._union(json.loads(answer["source_segments_json"]), op.source_segments)), now(), answer["id"]))
                                    self.conn.execute("UPDATE live_note_questions SET resolution_evidence_json=?,revision=revision+1,updated_at=? WHERE id=?", (json.dumps(combined), now(), q["id"]))
                                outcome.update({"question_id": q["id"], "answer_node_id": answer["id"], "duplicate_resolution": True})
                            else:
                                expected_answer_revision = op.expected_revisions.get(answer["id"])
                                if expected_answer_revision is None:
                                    for context_topic in context.get("related_topics", []):
                                        context_block = next((item for item in context_topic.get("blocks", []) if item.get("id") == answer["id"]), None)
                                        if context_block:
                                            expected_answer_revision = context_block.get("revision")
                                            break
                                ai_current = answer["owner"] == "ai" and not answer["protected"] and expected_answer_revision == answer["revision"]
                                if ai_current:
                                    old_id = uid()
                                    pos = self.conn.execute("SELECT coalesce(max(position),-1)+1 FROM live_note_blocks WHERE topic_id=?", (answer["topic_id"],)).fetchone()[0]
                                    self.conn.execute(
                                        "INSERT INTO live_note_blocks(id,topic_id,kind,content_md,owner,position,revision,source_segments_json,deleted_at,created_at,updated_at,semantic_role,epistemic_state,resolution_state,user_touched,protected,verified_against_source,aliases_json,canonical_terms_json,superseded_by) "
                                        "SELECT ?,topic_id,kind,content_md,owner,?,revision,source_segments_json,?,?,?,semantic_role,'superseded',resolution_state,user_touched,protected,verified_against_source,aliases_json,canonical_terms_json,? FROM live_note_blocks WHERE id=?",
                                        (old_id, pos, now(), now(), now(), answer["id"], answer["id"]),
                                    )
                                    sources = self._union(json.loads(answer["source_segments_json"]), prior_evidence, op.source_segments)
                                    self.conn.execute(
                                        "UPDATE live_note_blocks SET content_md=?,semantic_role=?,epistemic_state=?,source_segments_json=?,aliases_json=?,canonical_terms_json=?,revision=revision+1,updated_at=? WHERE id=?",
                                        (op.content_md, op.semantic_role or "conclusion", op.epistemic_state or "supported", json.dumps(sources), json.dumps(self._union(json.loads(answer["aliases_json"]), op.aliases)), json.dumps(self._union(json.loads(answer["canonical_terms_json"]), op.canonical_terms)), now(), answer["id"]),
                                    )
                                    self.conn.execute("UPDATE live_note_questions SET resolution_evidence_json=?,revision=revision+1,updated_at=? WHERE id=?", (json.dumps(combined), now(), q["id"]))
                                    outcome.update({"question_id": q["id"], "answer_node_id": answer["id"], "refined_answer": True, "superseded_answer_id": old_id})
                                else:
                                    reason = "回答済みの人間編集を保護するため差分提案として保存しました" if answer["owner"] == "human" or answer["protected"] else "回答済みAIノートが更新されたため新しい回答候補を保留しました"
                                    proposal = SemanticOperation.model_validate({
                                        "op_id": op.op_id, "type": "SUGGEST_EDIT", "target_type": "block",
                                        "target_id": answer["id"], "expected_revision": answer["revision"],
                                        "semantic_role": op.semantic_role or answer["semantic_role"],
                                        "epistemic_state": op.epistemic_state or "supported", "content_md": op.content_md,
                                        "source_segments": op.source_segments, "aliases": op.aliases, "canonical_terms": op.canonical_terms,
                                    })
                                    created = self._semantic_suggestion(sid, proposal, reason, "block", answer["id"])
                                    if not created.get("duplicate"):
                                        conflicts.append(created)
                                    outcome.update({"question_id": q["id"], "answer_node_id": answer["id"], "suggestion_id": created["id"]})
                        else:
                            if q["state"] == "deferred":
                                topic = self._topic(sid, q["topic_id"])
                                proposal = SemanticOperation.model_validate({
                                    "op_id": op.op_id, "type": "SUGGEST_EDIT", "target_type": "topic",
                                    "target_id": topic["id"], "expected_revision": topic["revision"],
                                    "semantic_role": op.semantic_role or "conclusion", "epistemic_state": op.epistemic_state or "supported",
                                    "content_md": op.content_md, "source_segments": op.source_segments,
                                    "aliases": op.aliases, "canonical_terms": op.canonical_terms,
                                })
                                created = self._semantic_suggestion(sid, proposal, "保留中の質問に新しい回答候補があります", "topic", topic["id"])
                                if not created.get("duplicate"):
                                    conflicts.append(created)
                                outcome["suggestion_id"] = created["id"]
                            else:
                                outcome["superseded_closed_question"] = True
                    elif q["revision"] != op.expected_revision:
                        # The evidence was generated against an earlier question row. It has no
                        # safe target revision, so fail the batch and let its bounded retry
                        # policy re-evaluate the evidence instead of advancing the cursor.
                        raise NoteError("REVISION_CONFLICT", "質問の状態が推論中に変わりました。発話を再評価します", 409)
                    else:
                        topic_id = temp_topics.get(op.topic_id, op.topic_id) if op.topic_id else q["topic_id"]
                        self._topic(sid, topic_id)
                        if not op.content_md or not op.content_md.strip():
                            raise NoteError("INVALID_PLAN", "回答本文が必要です")
                        ans_id = op.block_id
                        skip_resolution = False
                        if ans_id:
                            answer = self._block(sid, ans_id)
                            expected_answer_revision = op.expected_revisions.get(ans_id)
                            if expected_answer_revision is None:
                                for context_topic in context.get("related_topics", []):
                                    context_block = next((item for item in context_topic.get("blocks", []) if item.get("id") == ans_id), None)
                                    if context_block:
                                        expected_answer_revision = context_block.get("revision")
                                        break
                            human_protected = answer["owner"] == "human" or answer["protected"]
                            stale_answer = expected_answer_revision is not None and answer["revision"] != expected_answer_revision
                            if human_protected or stale_answer:
                                reason = "人間が編集した回答を保護するため差分提案として保存しました" if human_protected else "AI回答が更新されたため新しい根拠を再確認してください"
                                proposal = SemanticOperation.model_validate({
                                    "op_id": op.op_id, "type": "SUGGEST_EDIT", "target_type": "block",
                                    "target_id": ans_id, "expected_revision": answer["revision"],
                                    "semantic_role": op.semantic_role or "conclusion", "epistemic_state": op.epistemic_state or "supported",
                                    "content_md": op.content_md, "source_segments": op.source_segments,
                                    "aliases": op.aliases, "canonical_terms": op.canonical_terms,
                                })
                                created = self._semantic_suggestion(sid, proposal, reason, "block", ans_id)
                                if not created.get("duplicate"):
                                    conflicts.append(created)
                                outcome["suggestion_id"] = created["id"]
                                outcome["deferred_to_current_answer_revision"] = answer["revision"]
                                skip_resolution = True
                        if not conflict and not skip_resolution:
                            if ans_id:
                                old = self._block(sid, ans_id)
                                sources = self._union(json.loads(old["source_segments_json"]), json.loads(q["resolution_evidence_json"]), op.source_segments)
                                old_aliases = json.loads(old["aliases_json"])
                                old_terms = json.loads(old["canonical_terms_json"])
                                self.conn.execute("UPDATE live_note_blocks SET content_md=?,semantic_role=?,epistemic_state=?,source_segments_json=?,aliases_json=?,canonical_terms_json=?,semantic_v2=1,revision=revision+1,updated_at=? WHERE id=?", (op.content_md, op.semantic_role or "conclusion", op.epistemic_state or "supported", json.dumps(sources), json.dumps(self._union(old_aliases,op.aliases)), json.dumps(self._union(old_terms,op.canonical_terms)), now(), ans_id))
                            else:
                                ans_id = self._new_block(topic_id, "point", op.content_md, "ai", self._union(json.loads(q["resolution_evidence_json"]), op.source_segments), semantic_v2=True)
                                self.conn.execute("UPDATE live_note_blocks SET semantic_role=?,epistemic_state=?,aliases_json=?,canonical_terms_json=?,semantic_v2=1 WHERE id=?", (op.semantic_role or "conclusion", op.epistemic_state or "supported", json.dumps(op.aliases), json.dumps(op.canonical_terms), ans_id))
                            self.conn.execute("UPDATE live_note_questions SET state='answered',answer_node_id=?,resolution_evidence_json=?,resolved_seq=?,revision=revision+1,updated_at=? WHERE id=?", (ans_id, json.dumps(self._union(json.loads(q["resolution_evidence_json"]), op.source_segments)), max(op.source_segments), now(), q["id"]))
                            if q["question_node_id"]:
                                self.conn.execute("UPDATE live_note_blocks SET resolution_state='answered',epistemic_state='superseded',superseded_by=?,revision=revision+1,updated_at=? WHERE id=?", (ans_id, now(), q["question_node_id"]))
                            outcome.update({"question_id": q["id"], "answer_node_id": ans_id, "resolution_evidence": self._union(json.loads(q["resolution_evidence_json"]), op.source_segments)})
                elif op.type == "REVISE_CLAIM":
                    if not op.block_id or not op.content_md:
                        raise NoteError("INVALID_PLAN", "訂正対象と新しい本文が必要です")
                    b = self._block(sid, op.block_id)
                    if b["owner"] == "human" or b["protected"]:
                        conflict = "人間が編集した内容を保護するため訂正を提案しました"
                    elif op.expected_revision is not None and op.expected_revision != b["revision"]:
                        conflict = "AIノートが更新されたため新しい根拠を保留しました"
                    else:
                        # Preserve the prior claim as a superseded historical node with its full evidence.
                        old_id = uid(); pos = self.conn.execute("SELECT coalesce(max(position),-1)+1 FROM live_note_blocks WHERE topic_id=?", (b["topic_id"],)).fetchone()[0]
                        self.conn.execute("INSERT INTO live_note_blocks(id,topic_id,kind,content_md,owner,position,revision,source_segments_json,deleted_at,created_at,updated_at,semantic_role,epistemic_state,resolution_state,user_touched,protected,verified_against_source,aliases_json,canonical_terms_json,superseded_by) SELECT ?,topic_id,kind,content_md,'ai',?,0,source_segments_json,?,?,?,semantic_role,'superseded',resolution_state,0,0,verified_against_source,aliases_json,canonical_terms_json,? FROM live_note_blocks WHERE id=?", (old_id,pos,now(),now(),now(),b["id"],b["id"]))
                        src = self._union(json.loads(b["source_segments_json"]), op.source_segments)
                        self.conn.execute("UPDATE live_note_blocks SET content_md=?,semantic_role=?,epistemic_state=?,source_segments_json=?,verified_against_source=?,semantic_v2=1,superseded_by=NULL,revision=revision+1,updated_at=? WHERE id=?", (op.content_md, op.semantic_role or b["semantic_role"], op.epistemic_state or "supported",json.dumps(src),int(bool(op.verified_against_source)),now(),b["id"]))
                        outcome.update({"block_id": b["id"], "superseded_block_id": old_id})
                elif op.type == "MERGE_BLOCKS":
                    ids = self._union(op.source_block_ids, [op.target_block_id] if op.target_block_id else [])
                    if len(ids) < 2 or not op.content_md:
                        raise NoteError("INVALID_PLAN", "統合する複数項目と統合本文が必要です")
                    blocks = [self._block(sid, bid) for bid in ids]
                    expected = op.expected_revisions
                    stale = set(expected) != {b["id"] for b in blocks} or any(b["owner"] != "ai" or b["protected"] or expected.get(b["id"]) != b["revision"] for b in blocks)
                    topics = {b["topic_id"] for b in blocks}
                    if stale or len(topics) != 1:
                        conflict = "人間編集・版競合・別topicを含むため統合を提案しました"
                    else:
                        target = next((b for b in blocks if b["id"] == op.target_block_id), blocks[0])
                        sources = self._union(*(json.loads(b["source_segments_json"]) for b in blocks), op.source_segments)
                        aliases = self._union(*(json.loads(b["aliases_json"]) for b in blocks), op.aliases)
                        terms = self._union(*(json.loads(b["canonical_terms_json"]) for b in blocks), op.canonical_terms)
                        # Validate all inputs before mutation, then update target and archive the rest atomically.
                        self.conn.execute("UPDATE live_note_blocks SET content_md=?,source_segments_json=?,aliases_json=?,canonical_terms_json=?,semantic_v2=1,revision=revision+1,updated_at=? WHERE id=?", (op.content_md,json.dumps(sources),json.dumps(aliases),json.dumps(terms),now(),target["id"]))
                        for b in blocks:
                            if b["id"] != target["id"]:
                                self.conn.execute("UPDATE live_note_blocks SET deleted_at=?,superseded_by=?,epistemic_state='superseded',revision=revision+1,updated_at=? WHERE id=?", (now(),target["id"],now(),b["id"]))
                        outcome.update({"target_block_id": target["id"], "merged_block_ids": ids, "source_segments": sources})
                elif op.type == "MOVE_BLOCK":
                    if not op.block_id or not op.target_topic_id:
                        raise NoteError("INVALID_PLAN", "移動対象と移動先が必要です")
                    b = self._block(sid, op.block_id); target_tid = temp_topics.get(op.target_topic_id, op.target_topic_id); self._topic(sid, target_tid)
                    if b["owner"] == "human" or b["protected"] or (op.expected_revision is not None and op.expected_revision != b["revision"]):
                        conflict = "人間編集または版競合のため移動を提案しました"
                    else:
                        pos = self.conn.execute("SELECT coalesce(max(position),-1)+1 FROM live_note_blocks WHERE topic_id=?", (target_tid,)).fetchone()[0]
                        self.conn.execute("UPDATE live_note_blocks SET topic_id=?,position=?,revision=revision+1,updated_at=? WHERE id=?", (target_tid,pos,now(),b["id"]))
                        outcome.update({"block_id": b["id"], "topic_id": target_tid})
                elif op.type == "RESTRUCTURE_TOPIC":
                    tids = self._union(op.source_topic_ids, [op.topic_id] if op.topic_id else [])
                    topics = [self._topic(sid, tid) for tid in tids]
                    blocks = [b for tid in tids for b in self.snapshot(sid)["topics"] if b["id"] == tid for b in b["blocks"]]
                    risky = any(t["title_owner"] == "human" for t in topics) or any(b["owner"] == "human" or b["kind"] in ("decision","proposal","concern") for b in blocks)
                    if any(op.expected_topic_revisions.get(t["id"]) != t["revision"] for t in topics):
                        conflict = "見出しが更新されているため構成変更を提案しました"
                    elif risky:
                        conflict = "人間編集または決定・懸念を含む構成変更は提案しました"
                    else:
                        if op.title and op.topic_id:
                            self.conn.execute("UPDATE live_note_topics SET title=?,revision=revision+1,updated_at=? WHERE id=?", (op.title,now(),op.topic_id))
                        if op.target_topic_id:
                            self._topic(sid, op.target_topic_id)
                            for block_id in op.block_order:
                                b = self._block(sid, block_id)
                                if b["topic_id"] not in tids:
                                    raise NoteError("INVALID_PLAN", "構成対象に含まれないblockです")
                                pos = op.block_order.index(block_id)
                                self.conn.execute("UPDATE live_note_blocks SET topic_id=?,position=?,revision=revision+1,updated_at=? WHERE id=?",(op.target_topic_id,pos,now(),block_id))
                        outcome.update({"topic_ids": tids, "title": op.title})
                elif op.type == "UPDATE_TAKEAWAY":
                    if not op.content_md or len(op.content_md) > 1200:
                        raise NoteError("INVALID_PLAN", "要点は1200文字以内で指定してください")
                    row = self.conn.execute(
                        "SELECT b.* FROM live_note_blocks b JOIN live_note_topics t ON t.id=b.topic_id "
                        "WHERE t.session_id=? AND t.deleted_at IS NULL AND t.archived_at IS NULL "
                        "AND b.deleted_at IS NULL AND b.is_takeaway=1 ORDER BY b.updated_at DESC LIMIT 1", (sid,)
                    ).fetchone()
                    if row and (row["owner"] == "human" or row["user_touched"] or row["protected"]):
                        proposal = SemanticOperation.model_validate({
                            "op_id": op.op_id, "type": "SUGGEST_EDIT", "target_type": "block",
                            "target_id": row["id"], "expected_revision": row["revision"],
                            "semantic_role": "conclusion", "epistemic_state": "tentative",
                            "content_md": op.content_md, "source_segments": op.source_segments,
                            "aliases": op.aliases, "canonical_terms": op.canonical_terms,
                        })
                        conflicts.append(self._semantic_suggestion(sid, proposal,
                            "人間が編集した要点を保護するため差分提案として保存しました", "block", row["id"]))
                        outcome["suggestion_id"] = conflicts[-1]["id"]
                    else:
                        if row:
                            self.conn.execute(
                                "UPDATE live_note_blocks SET content_md=?,semantic_role='conclusion',epistemic_state='supported',"
                                "source_segments_json=?,aliases_json=?,canonical_terms_json=?,semantic_v2=1,revision=revision+1,updated_at=? WHERE id=?",
                                (op.content_md, json.dumps(self._union(json.loads(row["source_segments_json"]), op.source_segments)),
                                 json.dumps(self._union(json.loads(row["aliases_json"]), op.aliases)),
                                 json.dumps(self._union(json.loads(row["canonical_terms_json"]), op.canonical_terms)), now(), row["id"])
                            )
                            bid = row["id"]
                        else:
                            topic = self._new_topic(sid, "会議の要点", "ai")
                            bid = self._new_block(topic, "point", op.content_md, "ai", op.source_segments, semantic_v2=True)
                            self.conn.execute("UPDATE live_note_blocks SET semantic_role='conclusion',epistemic_state='supported',aliases_json=?,canonical_terms_json=?,is_takeaway=1 WHERE id=?", (json.dumps(op.aliases),json.dumps(op.canonical_terms),bid))
                        outcome.update({"takeaway": op.content_md, "block_id": bid})
                elif op.type == "SUGGEST_EDIT":
                    if not op.target_type or not op.target_id or not op.content_md:
                        raise NoteError("INVALID_PLAN", "提案対象と本文が必要です")
                    if op.target_type == "block": self._block(sid,op.target_id)
                    elif op.target_type == "topic": self._topic(sid,op.target_id)
                    else: raise NoteError("INVALID_PLAN", "document全体への提案は許可されません")
                    conflicts.append(self._semantic_suggestion(sid,op,"人間の編集を保護するため差分提案として保存しました",op.target_type,op.target_id))
                    outcome["suggestion_id"] = conflicts[-1]["id"]
                if conflict:
                    target_id = op.block_id or op.target_block_id or op.target_id
                    candidate_md = op.content_md
                    stale_ai = "AIノートが更新" in conflict
                    target = None
                    if target_id:
                        try:
                            target = self._block(sid, target_id)
                        except NoteError:
                            target = None
                    if stale_ai and target and candidate_md and op.type in ("UPSERT_KNOWLEDGE", "REVISE_CLAIM", "MERGE_BLOCKS"):
                        proposal = SemanticOperation.model_validate({
                            "op_id": op.op_id, "type": "SUGGEST_EDIT", "target_type": "block",
                            "target_id": target["id"], "expected_revision": target["revision"],
                            "semantic_role": op.semantic_role or target["semantic_role"],
                            "epistemic_state": op.epistemic_state or "supported", "content_md": candidate_md,
                            "source_segments": op.source_segments, "aliases": op.aliases, "canonical_terms": op.canonical_terms,
                        })
                        created = self._semantic_suggestion(sid, proposal, conflict, "block", target["id"])
                        if not created.get("duplicate"):
                            conflicts.append(created)
                        outcome["suggestion_id"] = created["id"]
                    elif stale_ai:
                        # Structural stale operations have no safe wording proposal. They are
                        # recorded as superseded in the operation outcome and reconsidered by a
                        # later consolidation pass against the current document.
                        outcome["superseded_stale_operation"] = True
                    else:
                        created = self._semantic_suggestion(sid,op,conflict,"block" if target_id else "topic",target_id or op.topic_id)
                        if not created.get("duplicate"):
                            conflicts.append(created)
                        outcome["suggestion_id"] = created["id"]
                applied.append(outcome)
                self.conn.execute("INSERT INTO live_note_applied_operations VALUES(?,?,?)", (op.op_id,batch["id"],"suggestion" if conflict else "applied"))
            self.conn.execute("UPDATE live_note_batches SET status='done',last_error=NULL,updated_at=? WHERE id=?", (now(),batch["id"]))
            self.conn.execute("UPDATE live_note_sessions SET last_processed_segment_seq=?,failures=0,next_attempt_at=0,last_error=NULL WHERE session_id=?", (max(cursor, batch["seq_to"]),sid))
            processed_cursor = max(cursor, batch["seq_to"])
            if current["kind"] == "finalize" and not self.conn.execute("SELECT 1 FROM live_note_segments WHERE session_id=? AND segment_seq>? LIMIT 1",(sid,processed_cursor)).fetchone():
                self.conn.execute("UPDATE live_note_sessions SET finalize_pass_done=1 WHERE session_id=?", (sid,))
            self._event(sid,"ai",applied + ([{"type":"SUGGESTIONS","items":conflicts}] if conflicts else []),before)
        return self.snapshot(sid), conflicts

    def _new_topic(self, sid, title, owner):
        if not title or not title.strip():
            raise NoteError("EMPTY_TITLE", "論点名を入力してください")
        tid = uid()
        pos = self.conn.execute(
            "SELECT coalesce(max(position),-1)+1 FROM live_note_topics WHERE session_id=?",
            (sid,),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO live_note_topics(id,session_id,title,title_owner,position,revision,deleted_at,created_at,updated_at) VALUES(?,?,?,?,?,0,NULL,?,?)",
            (tid, sid, title.strip(), owner, pos, now(), now()),
        )
        return tid

    def _new_block(self, tid, kind, text, owner, sources, semantic_v2=False):
        if text is None or not text.strip() or not kind:
            raise NoteError("EMPTY_BLOCK", "本文と種別が必要です")
        bid = uid()
        pos = self.conn.execute(
            "SELECT coalesce(max(position),-1)+1 FROM live_note_blocks WHERE topic_id=?",
            (tid,),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO live_note_blocks(id,topic_id,kind,content_md,owner,position,revision,source_segments_json,deleted_at,created_at,updated_at,semantic_v2) VALUES(?,?,?,?,?,?,0,?,NULL,?,?,?)",
            (bid, tid, kind, text, owner, pos, json.dumps(sources), now(), now(), int(semantic_v2)),
        )
        return bid

    def human(self, sid, action, body: HumanOperation, target=None):
        self.session(sid)
        payload = json.dumps([action, target, body.model_dump()], sort_keys=True)
        with self.conn:
            old = self.conn.execute(
                "SELECT payload FROM live_note_requests WHERE session_id=? AND request_id=?",
                (sid, body.request_id),
            ).fetchone()
            if old:
                if old[0] != payload:
                    raise NoteError(
                        "REQUEST_ID_REUSED", "リクエストIDが再利用されました", 409
                    )
                return self.snapshot(sid)
            before = self.snapshot(sid, True)
            if action == "create_topic":
                self._new_topic(sid, body.title, "human")
            elif action == "add_block":
                self._topic(sid, body.topic_id)
                bid = self._new_block(
                    body.topic_id, body.kind or "point", body.content_md, "human", []
                )
                role = {"question": "open_question", "decision": "decision", "action": "action", "concern": "risk", "proposal": "option"}.get(body.kind or "point", "note")
                self.conn.execute("UPDATE live_note_blocks SET semantic_role=?,user_touched=1,protected=1,epistemic_state='unknown' WHERE id=?", (role,bid))
            elif action in ("edit_topic", "delete_topic"):
                t = self._topic(sid, target)
                self._revision(t, body.expected_revision)
                if action == "edit_topic":
                    if not body.title or not body.title.strip():
                        raise NoteError("EMPTY_TITLE", "論点名を入力してください")
                    self.conn.execute(
                        "UPDATE live_note_topics SET title=?,title_owner='human',revision=revision+1,updated_at=? WHERE id=?",
                        (body.title.strip(), now(), target),
                    )
                else:
                    self.conn.execute(
                        "UPDATE live_note_topics SET deleted_at=?,revision=revision+1,updated_at=? WHERE id=?",
                        (now(), now(), target),
                    )
                    self.conn.execute(
                        "UPDATE live_note_sessions SET active_topic_id=NULL WHERE session_id=? AND active_topic_id=?",
                        (sid, target),
                    )
            elif action in ("edit_block", "delete_block"):
                b = self._block(sid, target)
                self._revision(b, body.expected_revision)
                if action == "edit_block":
                    if body.content_md is None:
                        raise NoteError("CONTENT_REQUIRED", "本文が必要です")
                    self.conn.execute(
                        "UPDATE live_note_blocks SET content_md=?,kind=?,owner='human',user_touched=1,protected=1,revision=revision+1,updated_at=? WHERE id=?",
                        (body.content_md, body.kind or b["kind"], now(), target),
                    )
                else:
                    self.conn.execute(
                        "UPDATE live_note_blocks SET deleted_at=?,owner='human',revision=revision+1,updated_at=? WHERE id=?",
                        (now(), now(), target),
                    )
            elif action == "reorder":
                if body.expected_revision != before["note_revision"]:
                    raise NoteError("REVISION_CONFLICT", "目次が更新されました", 409)
                ids = [t["id"] for t in before["topics"] if not t["deleted_at"]]
                if (
                    not body.topic_ids
                    or len(set(body.topic_ids)) != len(ids)
                    or set(ids) != set(body.topic_ids)
                ):
                    raise NoteError(
                        "INVALID_ORDER", "現在の全論点を一度ずつ指定してください"
                    )
                for pos, tid in enumerate(body.topic_ids):
                    self.conn.execute(
                        "UPDATE live_note_topics SET position=?,revision=revision+1,updated_at=? WHERE id=?",
                        (pos, now(), tid),
                    )
            elif action == "undo":
                self._undo(sid, target, body, before)
            elif action == "resolve":
                self._resolve(sid, target, body, before)
            else:
                raise NoteError("UNKNOWN_OPERATION", "不明な操作です")
            self.conn.execute(
                "INSERT INTO live_note_requests VALUES(?,?,?)",
                (sid, body.request_id, payload),
            )
            self._event(
                sid,
                "human",
                [{"action": action, "target_id": target, "request": body.model_dump()}],
                before,
            )
        return self.snapshot(sid)

    def document_edits(self, sid, body: DocumentEditsRequest):
        """Apply a multi-unit editor delta as one human event and one note revision."""
        self.session(sid)
        payload = json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=False)
        id_map = {}
        for operation in body.operations:
            if operation.client_id:
                if operation.client_id in id_map:
                    raise NoteError("INVALID_DOCUMENT_EDIT", "client_idが重複しています")
                id_map[operation.client_id] = uuid.uuid5(uuid.NAMESPACE_URL, f"seam:{sid}:{body.request_id}:{operation.client_id}").hex
        with self.conn:
            prior = self.conn.execute("SELECT payload FROM live_note_requests WHERE session_id=? AND request_id=?", (sid, body.request_id)).fetchone()
            if prior:
                if prior[0] != json.dumps(["document_edits", None, body.model_dump()], sort_keys=True):
                    # Keep retries bound to the exact original request body.
                    try:
                        stored = json.loads(prior[0])
                    except Exception:
                        stored = None
                    if stored != ["document_edits", None, body.model_dump()]:
                        raise NoteError("REQUEST_ID_REUSED", "リクエストIDが再利用されました", 409)
                return {"snapshot": self.snapshot(sid), "id_map": id_map}
            before = self.snapshot(sid, True)
            if body.expected_note_revision != before["note_revision"]:
                raise NoteError("REVISION_CONFLICT", "ノートが更新されました", 409, before)

            planned_topics = {op.client_id for op in body.operations if op.op == "create_topic"}
            topic_create_positions = {op.client_id: index for index, op in enumerate(body.operations) if op.op == "create_topic"}
            entity_writes = set()
            deleted_topics = {op.id for op in body.operations if op.op == "delete_topic"}
            deleted_client_topics = {op.client_id for op in body.operations if op.op == "create_topic" and any(
                later.op == "delete_topic" and later.id == op.client_id for later in body.operations
            )}
            for index, op in enumerate(body.operations):
                if op.op in ("update_topic", "delete_topic"):
                    key = ("topic", op.id)
                    if key in entity_writes:
                        raise NoteError("INVALID_DOCUMENT_EDIT", "同じ論点を複数回変更できません")
                    entity_writes.add(key)
                elif op.op in ("update_block", "delete_block"):
                    key = ("block", op.id)
                    if key in entity_writes:
                        raise NoteError("INVALID_DOCUMENT_EDIT", "同じ項目を複数回変更できません")
                    entity_writes.add(key)
                elif op.op == "create_block" and op.client_topic_id:
                    if op.client_topic_id in deleted_client_topics or op.client_topic_id not in topic_create_positions or topic_create_positions[op.client_topic_id] >= index:
                        raise NoteError("INVALID_DOCUMENT_EDIT", "項目を作る前に論点を作成してください")
                elif op.op == "create_block" and op.topic_id in deleted_topics:
                    raise NoteError("INVALID_DOCUMENT_EDIT", "削除対象の論点に項目を作成できません")
            active_topic_ids = {t["id"] for t in before["topics"] if not t["deleted_at"]}
            active_block_ids = {b["id"] for t in before["topics"] for b in t["blocks"] if not b["deleted_at"]}
            for op in body.operations:
                if op.op in ("update_topic", "delete_topic"):
                    topic = self._topic(sid, op.id)
                    self._revision(topic, op.expected_revision)
                elif op.op in ("update_block", "delete_block"):
                    block = self._block(sid, op.id)
                    self._revision(block, op.expected_revision)
                elif op.op == "create_block":
                    tid = id_map.get(op.client_topic_id) if op.client_topic_id else op.topic_id
                    if op.client_topic_id and op.client_topic_id not in planned_topics:
                        raise NoteError("INVALID_DOCUMENT_EDIT", "作成対象topicがありません")
                    if tid and tid not in id_map.values():
                        self._topic(sid, tid)
                elif op.op == "reorder_topics":
                    resolved = [id_map.get(x, x) for x in op.ids]
                    final_ids = active_topic_ids | {id_map[x] for x in planned_topics}
                    deleted = {op.id for op in body.operations if op.op == "delete_topic"}
                    if len(set(resolved)) != len(resolved) or set(resolved) != final_ids - deleted:
                        raise NoteError("INVALID_ORDER", "並べ替えには現在の全論点を一度ずつ指定してください")

            edits = []
            for op in body.operations:
                if op.op == "update_topic":
                    self.conn.execute("UPDATE live_note_topics SET title=?,title_owner='human',revision=revision+1,updated_at=? WHERE id=?", (op.title.strip(), now(), op.id))
                    edits.append({"op":op.op,"id":op.id})
                elif op.op == "update_block":
                    block = self._block(sid, op.id)
                    if op.markdown is None:
                        raise NoteError("CONTENT_REQUIRED", "本文が必要です")
                    kind = op.kind or block["kind"]
                    role = {"question":"open_question","decision":"decision","action":"action","concern":"risk","proposal":"option"}.get(kind,"note")
                    self.conn.execute("UPDATE live_note_blocks SET content_md=?,kind=?,semantic_role=?,semantic_v2=1,owner='human',user_touched=1,protected=1,revision=revision+1,updated_at=? WHERE id=?", (op.markdown,kind,role,now(),op.id))
                    edits.append({"op":op.op,"id":op.id})
                elif op.op == "create_topic":
                    tid = id_map[op.client_id]
                    self.conn.execute("UPDATE live_note_topics SET position=position+1,revision=revision+1,updated_at=? WHERE session_id=? AND deleted_at IS NULL AND position>=?", (now(),sid,op.position))
                    self.conn.execute("INSERT INTO live_note_topics(id,session_id,title,title_owner,position,revision,created_at,updated_at) VALUES(?,?,?,'human',?,0,?,?)", (tid,sid,op.title.strip(),op.position,now(),now()))
                    edits.append({"op":op.op,"id":tid,"client_id":op.client_id})
                elif op.op == "create_block":
                    bid = id_map[op.client_id]
                    tid = id_map.get(op.client_topic_id) if op.client_topic_id else op.topic_id
                    self.conn.execute("UPDATE live_note_blocks SET position=position+1,revision=revision+1,updated_at=? WHERE topic_id=? AND deleted_at IS NULL AND position>=?", (now(),tid,op.position))
                    role = {"question":"open_question","decision":"decision","action":"action","concern":"risk","proposal":"option"}.get(op.kind,"note")
                    self.conn.execute("INSERT INTO live_note_blocks(id,topic_id,kind,content_md,owner,position,revision,source_segments_json,created_at,updated_at,semantic_role,user_touched,protected,semantic_v2) VALUES(?,?,?,?,'human',?,0,'[]',?,?,?,1,1,1)", (bid,tid,op.kind,op.markdown,op.position,now(),now(),role))
                    edits.append({"op":op.op,"id":bid,"client_id":op.client_id})
                elif op.op == "delete_topic":
                    self.conn.execute("UPDATE live_note_topics SET deleted_at=?,revision=revision+1,updated_at=? WHERE id=?",(now(),now(),op.id))
                    self.conn.execute("UPDATE live_note_sessions SET active_topic_id=NULL WHERE session_id=? AND active_topic_id=?",(sid,op.id))
                    edits.append({"op":op.op,"id":op.id})
                elif op.op == "delete_block":
                    self.conn.execute("UPDATE live_note_blocks SET deleted_at=?,owner='human',user_touched=1,protected=1,revision=revision+1,updated_at=? WHERE id=?",(now(),now(),op.id))
                    edits.append({"op":op.op,"id":op.id})
                elif op.op == "reorder_topics":
                    for pos, tid in enumerate(id_map.get(x, x) for x in op.ids):
                        self.conn.execute("UPDATE live_note_topics SET position=?,title_owner='human',revision=revision+1,updated_at=? WHERE id=?",(pos,now(),tid))
                    edits.append({"op":op.op,"ids":[id_map.get(x,x) for x in op.ids]})
            self.conn.execute("INSERT INTO live_note_requests VALUES(?,?,?)", (sid,body.request_id,json.dumps(["document_edits",None,body.model_dump()],sort_keys=True)))
            self._event(sid,"human",[{"action":"document_edits","operations":edits,"id_map":id_map}],before)
        return {"snapshot":self.snapshot(sid),"id_map":id_map}

    def _undo(self, sid, eid, body, current):
        if body.expected_revision != current["note_revision"]:
            raise NoteError("REVISION_CONFLICT", "履歴が更新されました", 409)
        e = self.conn.execute(
            "SELECT * FROM live_note_events WHERE id=? AND session_id=?", (eid, sid)
        ).fetchone()
        if not e or not e["undo_json"]:
            raise NoteError("UNDO_CONFLICT", "取り消せる編集がありません", 409)
        previous = json.loads(e["undo_json"])
        # Compare before/after revisions from the next event (or current snapshot).
        next_event = self.conn.execute(
            "SELECT undo_json FROM live_note_events WHERE session_id=? AND note_revision=?",
            (sid, e["note_revision"] + 1),
        ).fetchone()
        after = json.loads(next_event[0]) if next_event and next_event[0] else current
        old_topics = {t["id"]: t for t in previous["topics"]}
        after_topics = {t["id"]: t for t in after["topics"]}
        current_topics = {t["id"]: t for t in current["topics"]}
        old_blocks = {b["id"]: b for t in previous["topics"] for b in t["blocks"]}
        after_blocks = {b["id"]: b for t in after["topics"] for b in t["blocks"]}
        current_blocks = {b["id"]: b for t in current["topics"] for b in t["blocks"]}
        old_questions = {q["id"]: q for q in previous.get("questions", [])}
        after_questions = {q["id"]: q for q in after.get("questions", [])}
        current_questions = {q["id"]: q for q in current.get("questions", [])}
        for qid, q_after in after_questions.items():
            q_before = old_questions.get(qid)
            if q_before is None or q_before.get("revision") != q_after.get("revision"):
                q_now = current_questions.get(qid)
                if not q_now or q_now.get("revision") != q_after.get("revision"):
                    raise NoteError("UNDO_CONFLICT", "質問には後から変更があります", 409)
                if q_before:
                    self.conn.execute("UPDATE live_note_questions SET state=?,answer_node_id=?,question_node_id=?,resolution_evidence_json=?,resolved_seq=?,revision=revision+1,updated_at=? WHERE id=?", (q_before["state"],q_before["answer_node_id"],q_before["question_node_id"],json.dumps(q_before["resolution_evidence"]),q_before["resolved_seq"],now(),qid))
                else:
                    self.conn.execute("DELETE FROM live_note_questions WHERE id=?", (qid,))
        if previous.get("legacy_takeaway") != after.get("legacy_takeaway"):
            if current.get("legacy_takeaway") != after.get("legacy_takeaway"):
                raise NoteError("UNDO_CONFLICT", "要点には後から変更があります", 409)
            metadata = json.loads(self.session(sid)["meeting_meta_json"])
            if previous.get("legacy_takeaway") is None:
                metadata.pop("takeaway", None)
            else:
                metadata["takeaway"] = previous["legacy_takeaway"]
            self.conn.execute("UPDATE live_note_sessions SET meeting_meta_json=? WHERE session_id=?", (json.dumps(metadata,ensure_ascii=False),sid))
        touched_topics = [
            key
            for key, row in after_topics.items()
            if key not in old_topics or row["revision"] != old_topics[key]["revision"]
        ]
        touched_blocks = [
            key
            for key, row in after_blocks.items()
            if key not in old_blocks or row["revision"] != old_blocks[key]["revision"]
        ]
        for key in touched_topics:
            row = current_topics.get(key)
            if not row or row["revision"] != after_topics[key]["revision"]:
                raise NoteError(
                    "UNDO_CONFLICT", "対象の見出しには後から変更があります", 409
                )
            # Undoing a topic creation must not erase blocks appended afterwards.
            if key not in old_topics and any(
                b["id"] not in after_blocks
                or b["revision"] != after_blocks[b["id"]]["revision"]
                for b in row["blocks"]
            ):
                raise NoteError(
                    "UNDO_CONFLICT", "論点に新しい項目があるため削除できません", 409
                )
        for key in touched_blocks:
            row = current_blocks.get(key)
            if not row or row["revision"] != after_blocks[key]["revision"]:
                raise NoteError(
                    "UNDO_CONFLICT", "対象の項目には後から変更があります", 409
                )
        for key in touched_topics:
            old = old_topics.get(key)
            if old:
                self.conn.execute(
                    "UPDATE live_note_topics SET title=?,title_owner=?,position=?,deleted_at=?,archived_at=?,digest=?,revision=revision+1,updated_at=? WHERE id=?",
                    (old["title"], old["title_owner"], old["position"], old["deleted_at"], old.get("archived_at"), old.get("digest", ""), now(), key),
                )
            else:
                self.conn.execute(
                    "UPDATE live_note_topics SET deleted_at=?,revision=revision+1 WHERE id=?",
                    (now(), key),
                )
        for key in touched_blocks:
            old = old_blocks.get(key)
            if old:
                self.conn.execute(
                    "UPDATE live_note_blocks SET topic_id=?,content_md=?,kind=?,owner=?,position=?,deleted_at=?,source_segments_json=?,semantic_role=?,epistemic_state=?,resolution_state=?,user_touched=?,protected=?,verified_against_source=?,aliases_json=?,canonical_terms_json=?,superseded_by=?,semantic_v2=?,is_takeaway=?,revision=revision+1,updated_at=? WHERE id=?",
                    (
                        old["topic_id"],
                        old["content_md"],
                        old["kind"],
                        old["owner"],
                        old["position"],
                        old["deleted_at"],
                        json.dumps(old["source_segment_seqs"]),
                        old["semantic_role"], old["epistemic_state"], old["resolution_state"],
                        int(old["user_touched"]), int(old["protected"]), int(old["verified_against_source"]),
                        json.dumps(old["aliases"]), json.dumps(old["canonical_terms"]), old["superseded_by"], int(old.get("semantic_v2", False)), int(old.get("is_takeaway", False)),
                        now(),
                        key,
                    ),
                )
            else:
                self.conn.execute(
                    "UPDATE live_note_blocks SET deleted_at=?,owner='human',revision=revision+1 WHERE id=?",
                    (now(), key),
                )
        active = previous["active_topic_id"]
        if (
            current["active_topic_id"] != active
            and current["active_topic_id"] == after["active_topic_id"]
        ):
            self.conn.execute(
                "UPDATE live_note_sessions SET active_topic_id=? WHERE session_id=?",
                (active, sid),
            )

    def _resolve(self, sid, suggestion_id, body, current):
        if body.expected_revision != current["note_revision"]:
            raise NoteError("REVISION_CONFLICT", "ノートが更新されました", 409)
        row = self.conn.execute(
            "SELECT * FROM live_note_suggestions WHERE id=? AND session_id=? AND status='pending'",
            (suggestion_id, sid),
        ).fetchone()
        if not row or body.action not in ("accept", "dismiss"):
            raise NoteError("SUGGESTION_NOT_FOUND", "未解決の提案がありません", 404)
        if body.action == "accept":
            op = json.loads(row["suggested_operation_json"])
            def stale_suggestion(message):
                # The surrounding human action is transactional. Persist only the lifecycle
                # transition, then return the existing 409 contract to the caller.
                self.conn.execute("UPDATE live_note_suggestions SET status='superseded' WHERE id=?", (suggestion_id,))
                self.conn.commit()
                raise NoteError("SUGGESTION_STALE", message, 409)
            if "type" in op:
                kind = op["type"]
                if kind in ("SUGGEST_EDIT", "UPSERT_KNOWLEDGE", "REVISE_CLAIM"):
                    target = op.get("target_id") or op.get("block_id")
                    target_type = op.get("target_type") or ("block" if target else None)
                    if kind == "UPSERT_KNOWLEDGE" and not target:
                        target = op["topic_id"]; target_type = "topic"
                    if target_type == "topic":
                        topic = self._topic(sid, target)
                        if op.get("expected_revision") != topic["revision"]:
                            stale_suggestion("見出しが更新されました。提案を再作成してください")
                        if kind == "SUGGEST_EDIT" and op.get("title"):
                            self.conn.execute("UPDATE live_note_topics SET title=?,title_owner='human',revision=revision+1,updated_at=? WHERE id=?",(op["title"],now(),topic["id"]))
                        elif op.get("content_md"):
                            role = op.get("semantic_role") or "note"
                            block_kind = {"open_question":"question","decision":"decision","action":"action","risk":"concern","option":"proposal"}.get(role,"point")
                            bid = self._new_block(topic["id"],block_kind,op["content_md"],"human",op.get("source_segments",[]))
                            self.conn.execute("UPDATE live_note_blocks SET semantic_role=?,user_touched=1,protected=1,epistemic_state=?,aliases_json=?,canonical_terms_json=? WHERE id=?",(role,op.get("epistemic_state") or "unknown",json.dumps(op.get("aliases",[])),json.dumps(op.get("canonical_terms",[])),bid))
                        else:
                            raise NoteError("SUGGESTION_UNSUPPORTED", "提案本文がありません")
                    elif target_type == "block":
                        block = self._block(sid,target)
                        if op.get("expected_revision") != block["revision"]:
                            stale_suggestion("項目が更新されました。提案を再作成してください")
                        if kind == "SUGGEST_EDIT" and op.get("content_md"):
                            text = op["content_md"]
                            role = op.get("semantic_role") or "note"
                        elif op.get("content_md"):
                            text = op["content_md"]
                            role = op.get("semantic_role") or block["semantic_role"]
                        else:
                            raise NoteError("SUGGESTION_UNSUPPORTED", "提案本文がありません")
                        # Acceptance applies the reviewed diff to the stable unit. Keep the
                        # prior wording as a superseded, evidence-bearing historical node.
                        historical_id = uid()
                        self.conn.execute(
                            "INSERT INTO live_note_blocks(id,topic_id,kind,content_md,owner,position,revision,source_segments_json,deleted_at,created_at,updated_at,semantic_role,epistemic_state,resolution_state,user_touched,protected,verified_against_source,aliases_json,canonical_terms_json,superseded_by) "
                            "SELECT ?,topic_id,kind,content_md,owner,position,revision+1,source_segments_json,?,created_at,?,semantic_role,'superseded',resolution_state,user_touched,protected,verified_against_source,aliases_json,canonical_terms_json,? FROM live_note_blocks WHERE id=?",
                            (historical_id, now(), now(), block["id"], block["id"]),
                        )
                        evidence = self._union(json.loads(block["source_segments_json"]), op.get("source_segments", []))
                        aliases = self._union(json.loads(block["aliases_json"]), op.get("aliases", []))
                        terms = self._union(json.loads(block["canonical_terms_json"]), op.get("canonical_terms", []))
                        block_kind = {"open_question":"question","decision":"decision","action":"action","risk":"concern","option":"proposal"}.get(role,"point")
                        self.conn.execute(
                            "UPDATE live_note_blocks SET content_md=?,kind=?,semantic_role=?,epistemic_state=?,source_segments_json=?,owner=?,user_touched=?,protected=?,aliases_json=?,canonical_terms_json=?,superseded_by=NULL,semantic_v2=1,revision=revision+1,updated_at=? WHERE id=?",
                            (text, block_kind, role, op.get("epistemic_state") or block["epistemic_state"], json.dumps(evidence), block["owner"], int(bool(block["user_touched"])), int(bool(block["protected"])), json.dumps(aliases), json.dumps(terms), now(), block["id"]),
                        )
                    else:
                        raise NoteError("SUGGESTION_UNSUPPORTED", "提案対象を特定できません")
                elif kind == "MERGE_BLOCKS":
                    ids = self._union(op.get("source_block_ids",[]),[op.get("target_block_id")] if op.get("target_block_id") else [])
                    blocks = [self._block(sid,bid) for bid in ids]
                    if len(ids)<2 or any(b["owner"]!="ai" or b["protected"] or op.get("expected_revisions",{}).get(b["id"])!=b["revision"] for b in blocks) or len({b["topic_id"] for b in blocks})!=1:
                        raise NoteError("SUGGESTION_STALE", "統合対象が更新されました。提案を再作成してください",409)
                    target = next(b for b in blocks if b["id"]==op["target_block_id"])
                    evidence=self._union(*(json.loads(b["source_segments_json"]) for b in blocks),op.get("source_segments",[]))
                    aliases=self._union(*(json.loads(b["aliases_json"]) for b in blocks),op.get("aliases",[]))
                    terms=self._union(*(json.loads(b["canonical_terms_json"]) for b in blocks),op.get("canonical_terms",[]))
                    self.conn.execute("UPDATE live_note_blocks SET content_md=?,source_segments_json=?,aliases_json=?,canonical_terms_json=?,owner='human',user_touched=1,protected=1,revision=revision+1,updated_at=? WHERE id=?",(op["content_md"],json.dumps(evidence),json.dumps(aliases),json.dumps(terms),now(),target["id"]))
                    for b in blocks:
                        if b["id"]!=target["id"]: self.conn.execute("UPDATE live_note_blocks SET deleted_at=?,superseded_by=?,epistemic_state='superseded',revision=revision+1,updated_at=? WHERE id=?",(now(),target["id"],now(),b["id"]))
                elif kind == "RESOLVE_QUESTION":
                    q = self.conn.execute("SELECT * FROM live_note_questions WHERE id=? AND session_id=?",(op.get("question_id"),sid)).fetchone()
                    if not q or q["state"] != "open" or q["revision"] != op.get("expected_revision"):
                        stale_suggestion("質問が更新されました。提案を再作成してください")
                    evidence=self._union(json.loads(q["resolution_evidence_json"]),op.get("source_segments",[]))
                    answer_id=self._new_block(q["topic_id"],"point",op["content_md"],"human",evidence)
                    self.conn.execute("UPDATE live_note_blocks SET semantic_role=?,epistemic_state=?,aliases_json=?,canonical_terms_json=?,user_touched=1,protected=1 WHERE id=?",(op.get("semantic_role") or "conclusion",op.get("epistemic_state") or "supported",json.dumps(op.get("aliases",[])),json.dumps(op.get("canonical_terms",[])),answer_id))
                    self.conn.execute("UPDATE live_note_questions SET state='answered',answer_node_id=?,resolution_evidence_json=?,resolved_seq=?,revision=revision+1,updated_at=? WHERE id=?",(answer_id,json.dumps(evidence),max(evidence,default=q["created_seq"]),now(),q["id"]))
                    if q["question_node_id"]:
                        self.conn.execute("UPDATE live_note_blocks SET resolution_state='answered',epistemic_state='superseded',superseded_by=?,revision=revision+1,updated_at=? WHERE id=?",(answer_id,now(),q["question_node_id"]))
                elif kind == "MOVE_BLOCK":
                    block=self._block(sid,op["block_id"]); target=self._topic(sid,op["target_topic_id"])
                    if block["revision"]!=op["expected_revision"]:
                        raise NoteError("SUGGESTION_STALE","項目が更新されました。提案を再作成してください",409)
                    pos=self.conn.execute("SELECT coalesce(max(position),-1)+1 FROM live_note_blocks WHERE topic_id=?",(target["id"],)).fetchone()[0]
                    self.conn.execute("UPDATE live_note_blocks SET topic_id=?,position=?,revision=revision+1,updated_at=? WHERE id=?",(target["id"],pos,now(),block["id"]))
                elif kind == "RESTRUCTURE_TOPIC":
                    tids=self._union(op.get("source_topic_ids",[]),[op.get("topic_id")] if op.get("topic_id") else [])
                    topics=[self._topic(sid,tid) for tid in tids]
                    if any(op.get("expected_topic_revisions",{}).get(t["id"])!=t["revision"] for t in topics):
                        raise NoteError("SUGGESTION_STALE","見出しが更新されました。提案を再作成してください",409)
                    target_id=op.get("target_topic_id")
                    if target_id: self._topic(sid,target_id)
                    if op.get("title") and op.get("topic_id"):
                        self.conn.execute("UPDATE live_note_topics SET title=?,title_owner='human',revision=revision+1,updated_at=? WHERE id=?",(op["title"],now(),op["topic_id"]))
                    for position,bid in enumerate(op.get("block_order",[])):
                        b=self._block(sid,bid)
                        if b["topic_id"] not in tids: raise NoteError("SUGGESTION_STALE","構成対象が変わりました",409)
                        self.conn.execute("UPDATE live_note_blocks SET topic_id=?,position=?,revision=revision+1,updated_at=? WHERE id=?",(target_id or b["topic_id"],position,now(),bid))
                    if target_id:
                        for topic in topics:
                            if topic["id"]!=target_id and not self.conn.execute("SELECT 1 FROM live_note_blocks WHERE topic_id=? AND deleted_at IS NULL",(topic["id"],)).fetchone():
                                self.conn.execute("UPDATE live_note_topics SET archived_at=?,revision=revision+1,updated_at=? WHERE id=?",(now(),now(),topic["id"]))
                else:
                    raise NoteError("SUGGESTION_UNSUPPORTED", "この提案は手動で編集してください")
                self.conn.execute("UPDATE live_note_suggestions SET status='accepted' WHERE id=?",(suggestion_id,))
                return
            # Acceptance adds a human-owned adjacent block; it never overwrites protected text.
            if op["action"] in ("add_block", "update_block"):
                self._topic(sid, op["topic_id"])
                self._new_block(
                    op["topic_id"],
                    op.get("kind") or "point",
                    op["markdown"],
                    "human",
                    op.get("source_segment_seqs", []),
                )
            elif op["action"] == "rename_topic":
                t = self._topic(sid, op["topic_id"])
                self.conn.execute(
                    "UPDATE live_note_topics SET title=?,title_owner='human',revision=revision+1 WHERE id=?",
                    (op["title"], t["id"]),
                )
            else:
                raise NoteError(
                    "SUGGESTION_UNSUPPORTED", "この提案は手動で編集してください"
                )
        self.conn.execute(
            "UPDATE live_note_suggestions SET status=? WHERE id=?",
            ("accepted" if body.action == "accept" else "dismissed", suggestion_id),
        )

    def event_operations(self, sid, revision):
        row = self.conn.execute(
            "SELECT operation_json FROM live_note_events WHERE session_id=? AND note_revision=?",
            (sid, revision),
        ).fetchone()
        return json.loads(row[0]) if row else []

    def history(self, sid, limit=50, offset=0):
        self.session(sid)
        rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM live_note_events WHERE session_id=? ORDER BY note_revision DESC LIMIT ? OFFSET ?",
                (sid, limit, offset),
            )
        ]
        return {
            "events": rows,
            "total": self.conn.execute(
                "SELECT count(*) FROM live_note_events WHERE session_id=?", (sid,)
            ).fetchone()[0],
        }

    def source(self, sid, segment_seq):
        self.session(sid)
        row = self.conn.execute("SELECT segment_seq,start_ms,end_ms,speaker_label,text FROM live_note_segments WHERE session_id=? AND segment_seq=?", (sid,segment_seq)).fetchone()
        if row is None:
            raise NoteError("SOURCE_NOT_FOUND", "発話の根拠がありません", 404)
        return dict(row)

    def batch(self, sid):
        s = self.session(sid)
        existing = self.conn.execute(
            "SELECT * FROM live_note_batches WHERE session_id=? AND status!='done' ORDER BY seq_from LIMIT 1",
            (sid,),
        ).fetchone()
        if existing:
            return dict(existing)
        limit = json.loads(s["live_config_json"]).get("max_batch_chars", 6000)
        rows = []
        chars = 0
        for r in self.conn.execute(
            "SELECT * FROM live_note_segments WHERE session_id=? AND segment_seq>? ORDER BY segment_seq",
            (sid, s["last_processed_segment_seq"]),
        ):
            if rows and chars + len(r["text"]) > limit:
                break
            rows.append(dict(r))
            chars += len(r["text"])
        if not rows:
            return None
        bid = f"{sid}:{rows[0]['segment_seq']}:{rows[-1]['segment_seq']}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO live_note_batches(id,session_id,seq_from,seq_to,status,provider,created_at,updated_at,kind) VALUES(?,?,?,?,'pending',?,?,?,?)",
                (
                    bid,
                    sid,
                    rows[0]["segment_seq"],
                    rows[-1]["segment_seq"],
                    s["provider"],
                    now(),
                    now(),
                    "finalize" if s["stopped"] else "fast",
                ),
            )
        return dict(
            self.conn.execute(
                "SELECT * FROM live_note_batches WHERE id=?", (bid,)
            ).fetchone()
        )

    def finalize_batch(self, sid):
        s = self.session(sid)
        if not s["stopped"] or s["finalize_pass_done"]:
            return None
        cursor = s["last_processed_segment_seq"]
        bid = f"{sid}:finalize:{cursor}"
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO live_note_batches(id,session_id,seq_from,seq_to,status,provider,created_at,updated_at,kind) VALUES(?,?,?,?,'pending',?,?,?,'finalize')", (bid,sid,cursor+1,cursor-1,s["provider"],now(),now()))
        row = self.conn.execute("SELECT * FROM live_note_batches WHERE id=?", (bid,)).fetchone()
        return dict(row) if row and row["status"] != "done" else None

    def consolidation_batch(self, sid):
        s = self.session(sid)
        if s["stopped"] or s["paused"] or self.snapshot(sid)["pending_segments"]:
            return None
        existing = self.conn.execute(
            "SELECT * FROM live_note_batches WHERE session_id=? AND kind='consolidate' AND status!='done' ORDER BY created_at DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if existing:
            return dict(existing)
        latest = self.conn.execute("SELECT max(created_at) FROM live_note_batches WHERE session_id=? AND kind='consolidate'",(sid,)).fetchone()[0]
        completed = self.conn.execute("SELECT context_json FROM live_note_batches WHERE session_id=? AND kind='consolidate' AND status='done' ORDER BY created_at DESC LIMIT 1", (sid,)).fetchone()
        if completed and completed[0]:
            try:
                if json.loads(completed[0]).get("_document_digest") == self._document_digest(sid):
                    return None
            except (TypeError, ValueError):
                pass
        baseline = datetime.fromisoformat(latest).timestamp() if latest else datetime.fromisoformat(s["started_at"]).timestamp()
        settings = json.loads(s["live_config_json"])
        if time.time() - baseline < settings.get("consolidation_interval_sec", 240):
            return None
        fast_count = self.conn.execute("SELECT count(*) FROM live_note_batches WHERE session_id=? AND kind='fast' AND status='done'",(sid,)).fetchone()[0]
        if fast_count < settings.get("min_consolidation_batches", 4):
            return None
        cursor=s["last_processed_segment_seq"]; bid=f"{sid}:consolidate:{cursor}:{int(time.time())}"
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO live_note_batches(id,session_id,seq_from,seq_to,status,provider,created_at,updated_at,kind) VALUES(?,?,?,?,'pending',?,?,?,'consolidate')",(bid,sid,cursor+1,cursor,s["provider"],now(),now()))
        row=self.conn.execute("SELECT * FROM live_note_batches WHERE id=?",(bid,)).fetchone()
        return dict(row) if row and row["status"]!='done' else None

    def context(self, sid, batch):
        snap = self.snapshot(sid)
        segments = [
            dict(r)
            for r in self.conn.execute(
                "SELECT segment_seq,start_ms,end_ms,speaker_label,text FROM live_note_segments WHERE session_id=? AND segment_seq BETWEEN ? AND ? ORDER BY segment_seq",
                (sid, batch["seq_from"], batch["seq_to"]),
            )
        ]
        if batch.get("kind") in ("finalize", "consolidate") and batch["seq_from"] > batch["seq_to"]:
            segments = [dict(r) for r in self.conn.execute(
                "SELECT segment_seq,start_ms,end_ms,speaker_label,text FROM live_note_segments WHERE session_id=? ORDER BY segment_seq DESC LIMIT 120", (sid,)
            )]
            segments.reverse()
        text = " ".join(r["text"] for r in segments)

        def tokens(t):
            return set(
                re.findall(r"[A-Za-z0-9_]+|[\u3040-\u30ff\u3400-\u9fff]{2}", t.lower())
            ) | {t[i : i + 2] for i in range(len(t) - 1) if not t[i : i + 2].isspace()}

        query = tokens(text)
        ranked = sorted(
            snap["topics"],
            key=lambda t: (
                len(
                    query
                    & tokens(
                        t["title"]
                        + " " + " ".join(
                            b["content_md"] + " " + " ".join(b.get("aliases", []))
                            + " " + " ".join(b.get("canonical_terms", []))
                            for b in t["blocks"]
                        )
                    )
                ),
                t["id"] == snap["active_topic_id"],
            ),
            reverse=True,
        )
        # Keep full outline for topic return, bounded excerpts for candidate blocks.
        # A long human document must not grow every prompt without limit.
        related = []
        remaining = 36000
        for topic in ranked[:5]:
            candidates = sorted(
                topic["blocks"],
                key=lambda b: (len(query & tokens(b["content_md"])), b["position"]),
                reverse=True,
            )[:12]
            blocks = []
            for block in sorted(candidates, key=lambda b: b["position"]):
                if remaining <= 0:
                    break
                text = block["content_md"][: min(2000, remaining)]
                remaining -= len(text)
                blocks.append(
                    {
                        **block,
                        "content_md": text,
                        "context_truncated": len(text) < len(block["content_md"]),
                    }
                )
            related.append({**topic, "blocks": blocks})
        relevant_topic_ids = {t["id"] for t in ranked[:5]}
        questions = [dict(q) for q in self.conn.execute("SELECT * FROM live_note_questions WHERE session_id=? AND state='open' ORDER BY created_seq,id", (sid,))]
        return {
            "schema_version": 2,
            "meeting": json.loads(self.session(sid)["meeting_meta_json"]),
            "started_at": snap["started_at"],
            "batch_id": batch["id"],
            "batch_kind": batch.get("kind", "fast"),
            "base_note_revision": snap["note_revision"],
            "base_revision": snap["note_revision"],
            "takeaway": snap.get("takeaway"),
            "open_questions": questions,
            "outline": [
                {
                    "id": t["id"],
                    "title": t["title"],
                    "title_owner": t["title_owner"],
                    "revision": t["revision"],
                    "digest": " ".join(b["content_md"] for b in t["blocks"])[:500],
                    "aliases": self._union(*(b.get("aliases", []) for b in t["blocks"])),
                    "canonical_terms": self._union(*(b.get("canonical_terms", []) for b in t["blocks"])),
                }
                for t in snap["topics"]
            ],
            "related_topics": related,
            "segments": segments,
        }

    def start_call(self, sid, batch, context, repair=False):
        settings = json.loads(self.session(sid)["live_config_json"])
        count = self.conn.execute(
            "SELECT count(*) FROM live_note_calls WHERE started_at>?",
            (time.time() - 3600,),
        ).fetchone()[0]
        if count >= settings["max_calls_per_hour"]:
            raise NoteError(
                "CALL_LIMIT", "呼び出し上限に達しました。発話は保存済みです", 429
            )
        cid = uid()
        with self.conn:
            self.conn.execute(
                "INSERT INTO live_note_calls(id,session_id,batch_id,started_at) VALUES(?,?,?,?)",
                (cid, sid, batch["id"], time.time()),
            )
            self.conn.execute(
                "UPDATE live_note_batches SET status='processing',attempts=attempts+1,repair_calls=repair_calls+?,context_json=?,updated_at=? WHERE id=?",
                (
                    int(repair),
                    json.dumps({**context, "_document_digest": self._document_digest(sid)}, ensure_ascii=False),
                    now(),
                    batch["id"],
                ),
            )
        return cid

    def _document_digest(self, sid):
        rows = self.conn.execute(
            "SELECT t.id,t.title,t.position,t.revision,b.id,b.content_md,b.position,b.revision,b.deleted_at "
            "FROM live_note_topics t LEFT JOIN live_note_blocks b ON b.topic_id=t.id "
            "WHERE t.session_id=? AND t.deleted_at IS NULL AND t.archived_at IS NULL "
            "ORDER BY t.position,t.id,b.position,b.id", (sid,)
        ).fetchall()
        payload = [tuple(row) for row in rows]
        return __import__("hashlib").sha256(json.dumps(payload,ensure_ascii=False).encode()).hexdigest()

    def finish_call(self, cid, duration, error=None):
        with self.conn:
            self.conn.execute(
                "UPDATE live_note_calls SET duration_ms=?,error=? WHERE id=?",
                (duration, error, cid),
            )

    def call_limit_reset(self):
        row = self.conn.execute(
            "SELECT min(started_at) FROM live_note_calls WHERE started_at>?",
            (time.time() - 3600,),
        ).fetchone()
        return (row[0] + 3600) if row[0] is not None else time.time()

    def last_call_time(self, sid):
        return self.conn.execute(
            "SELECT coalesce(max(started_at),0) FROM live_note_calls WHERE session_id=?",
            (sid,),
        ).fetchone()[0]

    def fail_batch(self, bid, error):
        with self.conn:
            self.conn.execute(
                "UPDATE live_note_batches SET status='failed',last_error=?,updated_at=? WHERE id=?",
                (error, now(), bid),
            )

    def recover(self):
        with self.conn:
            self.conn.execute(
                "UPDATE live_note_batches SET status='pending' WHERE status='processing'"
            )
            # Recording devices do not resume themselves after a process restart.
            self.conn.execute(
                "UPDATE live_note_sessions SET stopped=1,status=CASE WHEN paused THEN 'paused' WHEN status='error' THEN 'error' ELSE 'finalizing' END,flush_requested=1 WHERE status NOT IN ('completed','aborted')"
            )
        return self.sessions()

    def apply(self, sid, batch, context, raw):
        if len(json.dumps(raw, ensure_ascii=False).encode()) > 131072:
            raise NoteError("INVALID_PLAN", "JSON出力が大きすぎます")
        if isinstance(raw, dict) and raw.get("schema_version") == 2:
            return self._apply_v2(sid, batch, context, raw)
        plan = OperationPlan.model_validate(raw)
        if (
            plan.batch_id != batch["id"]
            or plan.base_note_revision != context["base_note_revision"]
        ):
            raise NoteError("INVALID_PLAN", "バッチまたは基準revisionが不一致です")
        s = self.session(sid)
        if (
            len(plan.operations)
            > json.loads(s["live_config_json"])["max_operations_per_batch"]
        ):
            raise NoteError("INVALID_PLAN", "操作数が上限を超えました")
        valid_sources = {r["segment_seq"]: r["text"] for r in context["segments"]}
        topic_revisions = {t["id"]: t["revision"] for t in context["outline"]}
        visible_blocks = {
            b["id"] for t in context["related_topics"] for b in t["blocks"]
        }
        ids = set()
        temp = {}
        applied = []
        conflicts = []
        with self.conn:
            current_batch = self.conn.execute(
                "SELECT * FROM live_note_batches WHERE id=? AND session_id=?",
                (batch["id"], sid),
            ).fetchone()
            if current_batch is None:
                raise NoteError(
                    "BATCH_NOT_FOUND", "会議に属するバッチがありません", 404
                )
            if current_batch["status"] == "done":
                return self.snapshot(sid), []
            cursor = self.session(sid)["last_processed_segment_seq"]
            if (
                batch["seq_from"] != current_batch["seq_from"]
                or batch["seq_to"] != current_batch["seq_to"]
                or batch["seq_from"] != cursor + 1
            ):
                raise NoteError(
                    "BATCH_RANGE_CONFLICT", "バッチの発話範囲が不一致です", 409
                )
            before = self.snapshot(sid, True)
            for op in plan.operations:
                if not op.op_id.startswith(batch["id"] + ":") or op.op_id in ids:
                    raise NoteError("INVALID_PLAN", "操作IDが不正です")
                ids.add(op.op_id)
                if self.conn.execute(
                    "SELECT 1 FROM live_note_applied_operations WHERE op_id=?",
                    (op.op_id,),
                ).fetchone():
                    raise NoteError("INVALID_PLAN", "別のバッチの操作IDです")
                if any(
                    isinstance(n, bool) or n not in valid_sources
                    for n in op.source_segment_seqs
                ):
                    raise NoteError(
                        "INVALID_EVIDENCE", "存在しない発話が参照されました"
                    )
                if (
                    op.action not in ("no_op", "set_active_topic")
                    and not op.source_segment_seqs
                ):
                    raise NoteError("INVALID_EVIDENCE", "発話の根拠が必要です")
                reason = None
                if op.action == "create_topic":
                    if (
                        not op.topic_id
                        or not re.fullmatch(r"new:\d+", op.topic_id)
                        or op.topic_id in temp
                        or not op.title
                        or not op.title.strip()
                    ):
                        raise NoteError(
                            "INVALID_PLAN", "仮論点IDまたは見出しが不正です"
                        )
                    normalized = lambda v: re.sub(r"\s+", "", v).casefold()
                    duplicate = next(
                        (
                            t
                            for t in self.snapshot(sid)["topics"]
                            if normalized(t["title"]) == normalized(op.title)
                        ),
                        None,
                    )
                    temp[op.topic_id] = (
                        duplicate["id"]
                        if duplicate
                        else self._new_topic(sid, op.title, "ai")
                    )
                    applied.append(
                        {
                            "action": "create_topic",
                            "topic": self._topic(sid, temp[op.topic_id]),
                        }
                    )
                elif op.action != "no_op":
                    tid = temp.get(op.topic_id, op.topic_id)
                    topic = self._topic(sid, tid)
                    data = op.model_dump()
                    data["topic_id"] = tid
                    if op.action in ("add_block", "update_block"):
                        if not op.kind or not op.markdown or not op.markdown.strip():
                            raise NoteError("INVALID_PLAN", "本文と種別が必要です")
                        evidence = " ".join(
                            valid_sources[n] for n in op.source_segment_seqs
                        )
                        if op.kind == "decision" and (
                            not re.search(
                                r"決定|決まり|合意|承認|確定|採用します|で進めます|でいきます|agree(?:d)?|decid(?:e|ed)|approved|confirmed",
                                evidence,
                                re.IGNORECASE,
                            )
                            or re.search(
                                r"未決|未確定|未承認|未合意|承認されていま|確定していま|確定していな|合意されていま|決定しな|決定は次回|決定でき|決定していな|決定していま|決定されていま|決定ではな|合意していな|合意していま|not (?:yet )?(?:agreed|decided|approved|confirmed)|提案|かもしれ|どうでしょ|と思う",
                                evidence,
                                re.IGNORECASE,
                            )
                        ):
                            reason = "明示的な決定根拠を確認できないため保留"
                    if op.action == "update_block":
                        if op.block_id not in visible_blocks:
                            raise NoteError(
                                "INVALID_PLAN", "参照していない項目への更新です"
                            )
                        block = self._block(sid, op.block_id)
                        if (
                            block["topic_id"] != tid
                            or op.expected_block_revision is None
                        ):
                            raise NoteError(
                                "INVALID_PLAN", "項目の所属またはrevisionが不正です"
                            )
                        if (
                            block["owner"] == "human"
                            or block["revision"] != op.expected_block_revision
                        ):
                            reason = "人間の編集または新しい変更を保護しました"
                        if (
                            not reason
                            and block["kind"] in ("decision", "proposal", "concern")
                            and block["kind"] != op.kind
                        ):
                            bid = self._new_block(
                                tid, op.kind, op.markdown, "ai", op.source_segment_seqs
                            )
                            applied.append(
                                {
                                    "action": "add_block",
                                    "topic_id": tid,
                                    "block": self._block(sid, bid),
                                }
                            )
                        elif not reason:
                            self.conn.execute(
                                "UPDATE live_note_blocks SET content_md=?,kind=?,source_segments_json=?,revision=revision+1,updated_at=? WHERE id=?",
                                (
                                    op.markdown,
                                    op.kind,
                                    json.dumps(
                                        sorted(
                                            set(
                                                json.loads(
                                                    block["source_segments_json"]
                                                )
                                                + op.source_segment_seqs
                                            )
                                        )
                                    ),
                                    now(),
                                    op.block_id,
                                ),
                            )
                            applied.append(
                                {
                                    "action": "update_block",
                                    "topic_id": tid,
                                    "block": self._block(sid, op.block_id),
                                }
                            )
                    elif op.action == "add_block" and not reason:
                        bid = self._new_block(
                            tid, op.kind, op.markdown, "ai", op.source_segment_seqs
                        )
                        applied.append(
                            {
                                "action": "add_block",
                                "topic_id": tid,
                                "block": self._block(sid, bid),
                            }
                        )
                    elif op.action == "rename_topic":
                        if not op.title or not op.title.strip():
                            raise NoteError("INVALID_PLAN", "見出しが必要です")
                        if topic["title_owner"] == "human" or topic[
                            "revision"
                        ] != topic_revisions.get(tid):
                            reason = "手動見出しまたは新しい変更を保護しました"
                        else:
                            self.conn.execute(
                                "UPDATE live_note_topics SET title=?,revision=revision+1,updated_at=? WHERE id=?",
                                (op.title, now(), tid),
                            )
                            applied.append(
                                {
                                    "action": "rename_topic",
                                    "topic": self._topic(sid, tid),
                                }
                            )
                    elif op.action == "set_active_topic":
                        self.conn.execute(
                            "UPDATE live_note_sessions SET active_topic_id=? WHERE session_id=?",
                            (tid, sid),
                        )
                        applied.append({"action": "set_active_topic", "topic_id": tid})
                    if reason:
                        suggestion_id = uid()
                        self.conn.execute(
                            "INSERT INTO live_note_suggestions VALUES(?,?,?,?,?,?,?,?)",
                            (
                                suggestion_id,
                                sid,
                                "block" if op.block_id else "topic",
                                op.block_id or tid,
                                json.dumps(data, ensure_ascii=False),
                                reason,
                                "pending",
                                now(),
                            ),
                        )
                        conflicts.append({"id": suggestion_id, "reason": reason})
                self.conn.execute(
                    "INSERT INTO live_note_applied_operations VALUES(?,?,?)",
                    (op.op_id, batch["id"], "suggestion" if reason else "applied"),
                )
            self.conn.execute(
                "UPDATE live_note_batches SET status='done',last_error=NULL,updated_at=? WHERE id=?",
                (now(), batch["id"]),
            )
            self.conn.execute(
                "UPDATE live_note_sessions SET last_processed_segment_seq=?,failures=0,next_attempt_at=0,last_error=NULL WHERE session_id=?",
                (max(cursor,batch["seq_to"]), sid),
            )
            current_kind = self.conn.execute("SELECT kind FROM live_note_batches WHERE id=?", (batch["id"],)).fetchone()[0]
            processed_cursor = max(cursor,batch["seq_to"])
            if current_kind == "finalize" and not self.conn.execute("SELECT 1 FROM live_note_segments WHERE session_id=? AND segment_seq>? LIMIT 1",(sid,processed_cursor)).fetchone():
                self.conn.execute("UPDATE live_note_sessions SET finalize_pass_done=1 WHERE session_id=?", (sid,))
            self._event(
                sid,
                "ai",
                applied
                + (
                    [{"action": "suggestions", "items": conflicts}] if conflicts else []
                ),
                before,
            )
        return self.snapshot(sid), conflicts

    def pending_chars(self, sid):
        s = self.session(sid)
        return self.conn.execute(
            "SELECT coalesce(sum(length(text)),0) FROM live_note_segments WHERE session_id=? AND segment_seq>?",
            (sid, s["last_processed_segment_seq"]),
        ).fetchone()[0]
