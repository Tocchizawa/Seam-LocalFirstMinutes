from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .models import HumanOperation, OperationPlan
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
 flush_requested INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS live_note_segments(
 session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 segment_seq INTEGER NOT NULL, source_id TEXT NOT NULL, start_ms INTEGER NOT NULL,
 end_ms INTEGER NOT NULL, speaker_label TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(session_id,segment_seq), UNIQUE(session_id,source_id));
CREATE TABLE IF NOT EXISTS live_note_topics(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 title TEXT NOT NULL, title_owner TEXT NOT NULL CHECK(title_owner IN ('human','ai')),
 position INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0, deleted_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_live_topics ON live_note_topics(session_id,position);
CREATE TABLE IF NOT EXISTS live_note_blocks(
 id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES live_note_topics(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('context','point','proposal','concern','decision','question','action')),
 content_md TEXT NOT NULL, owner TEXT NOT NULL CHECK(owner IN ('human','ai')),
 position INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 source_segments_json TEXT NOT NULL DEFAULT '[]', deleted_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_live_blocks ON live_note_blocks(topic_id,position);
CREATE TABLE IF NOT EXISTS live_note_batches(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES live_note_sessions(session_id) ON DELETE CASCADE,
 seq_from INTEGER NOT NULL, seq_to INTEGER NOT NULL, status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, repair_calls INTEGER NOT NULL DEFAULT 0,
 provider TEXT NOT NULL, last_error TEXT, context_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
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
"""


def migrate(conn):
    conn.executescript(SCHEMA)
    if "meeting_meta_json" not in {
        row[1] for row in conn.execute("PRAGMA table_info(live_note_sessions)")
    }:
        conn.execute(
            "ALTER TABLE live_note_sessions ADD COLUMN meeting_meta_json TEXT NOT NULL DEFAULT '{}'"
        )
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
        for key in ("provider_config_json", "live_config_json", "meeting_meta_json"):
            s.pop(key, None)
        s["topics"] = []
        suffix = "" if include_deleted else " AND deleted_at IS NULL"
        for r in self.conn.execute(
            "SELECT * FROM live_note_topics WHERE session_id=?"
            + suffix
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
                topic["blocks"].append(block)
            s["topics"].append(topic)
        s["suggestions"] = [
            dict(r)
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
        return render_markdown(self.snapshot(sid)["topics"])

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

    def _new_topic(self, sid, title, owner):
        if not title or not title.strip():
            raise NoteError("EMPTY_TITLE", "論点名を入力してください")
        tid = uid()
        pos = self.conn.execute(
            "SELECT coalesce(max(position),-1)+1 FROM live_note_topics WHERE session_id=?",
            (sid,),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO live_note_topics VALUES(?,?,?,?,?,0,NULL,?,?)",
            (tid, sid, title.strip(), owner, pos, now(), now()),
        )
        return tid

    def _new_block(self, tid, kind, text, owner, sources):
        if text is None or not text.strip() or not kind:
            raise NoteError("EMPTY_BLOCK", "本文と種別が必要です")
        bid = uid()
        pos = self.conn.execute(
            "SELECT coalesce(max(position),-1)+1 FROM live_note_blocks WHERE topic_id=?",
            (tid,),
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO live_note_blocks VALUES(?,?,?,?,?,?,0,?,NULL,?,?)",
            (bid, tid, kind, text, owner, pos, json.dumps(sources), now(), now()),
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
                self._new_block(
                    body.topic_id, body.kind or "point", body.content_md, "human", []
                )
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
                        "UPDATE live_note_blocks SET content_md=?,kind=?,owner='human',revision=revision+1,updated_at=? WHERE id=?",
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
                    "UPDATE live_note_topics SET title=?,title_owner='human',position=?,deleted_at=?,revision=revision+1,updated_at=? WHERE id=?",
                    (old["title"], old["position"], old["deleted_at"], now(), key),
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
                    "UPDATE live_note_blocks SET content_md=?,kind=?,owner='human',position=?,deleted_at=?,source_segments_json=?,revision=revision+1,updated_at=? WHERE id=?",
                    (
                        old["content_md"],
                        old["kind"],
                        old["position"],
                        old["deleted_at"],
                        json.dumps(old["source_segment_seqs"]),
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
                "INSERT INTO live_note_batches(id,session_id,seq_from,seq_to,status,provider,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?,?)",
                (
                    bid,
                    sid,
                    rows[0]["segment_seq"],
                    rows[-1]["segment_seq"],
                    s["provider"],
                    now(),
                    now(),
                ),
            )
        return dict(
            self.conn.execute(
                "SELECT * FROM live_note_batches WHERE id=?", (bid,)
            ).fetchone()
        )

    def context(self, sid, batch):
        snap = self.snapshot(sid)
        segments = [
            dict(r)
            for r in self.conn.execute(
                "SELECT segment_seq,start_ms,end_ms,speaker_label,text FROM live_note_segments WHERE session_id=? AND segment_seq BETWEEN ? AND ? ORDER BY segment_seq",
                (sid, batch["seq_from"], batch["seq_to"]),
            )
        ]
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
                        + " "
                        + " ".join(b["content_md"] for b in t["blocks"])
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
        return {
            "schema_version": 1,
            "meeting": json.loads(self.session(sid)["meeting_meta_json"]),
            "started_at": snap["started_at"],
            "batch_id": batch["id"],
            "base_note_revision": snap["note_revision"],
            "outline": [
                {
                    "id": t["id"],
                    "title": t["title"],
                    "title_owner": t["title_owner"],
                    "revision": t["revision"],
                    "digest": " ".join(b["content_md"] for b in t["blocks"])[:100],
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
                    json.dumps(context, ensure_ascii=False),
                    now(),
                    batch["id"],
                ),
            )
        return cid

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
            if (
                batch["seq_from"] != current_batch["seq_from"]
                or batch["seq_to"] != current_batch["seq_to"]
                or batch["seq_from"]
                != self.session(sid)["last_processed_segment_seq"] + 1
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
                (batch["seq_to"], sid),
            )
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
