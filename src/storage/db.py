from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

from src.config import APP_DIR

logger = logging.getLogger(__name__)

DB_PATH = APP_DIR / "minutes.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS minutes (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL UNIQUE,
    session_id      TEXT NOT NULL UNIQUE,
    project_id      TEXT NOT NULL,
    title           TEXT NOT NULL,
    date            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    duration_sec    INTEGER NOT NULL,
    transcript      TEXT NOT NULL,
    transcript_text TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL,
    context_snapshot TEXT,
    llm_model       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_minutes_project_id ON minutes(project_id);
CREATE INDEX IF NOT EXISTS idx_minutes_date ON minutes(date);
-- 一覧表示クエリ "WHERE project_id=? ORDER BY date DESC, started_at DESC" を
-- インデックスのみで処理できるようにする。
CREATE INDEX IF NOT EXISTS idx_minutes_project_date_started
    ON minutes(project_id, date DESC, started_at DESC);

CREATE TABLE IF NOT EXISTS meeting_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    start       REAL NOT NULL,
    number      INTEGER NOT NULL,
    body        TEXT NOT NULL,
    title       TEXT,
    auto_title  TEXT,
    UNIQUE(session_id, start)
);

CREATE TABLE IF NOT EXISTS meeting_chats (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    title       TEXT,
    messages    TEXT NOT NULL DEFAULT '[]',
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_meeting_chats_session ON meeting_chats(session_id, updated_at);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS minutes_fts USING fts5(
    title,
    summary,
    transcript_text,
    content='minutes',
    content_rowid='rowid',
    tokenize='trigram'
);
"""

TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS minutes_ai AFTER INSERT ON minutes BEGIN
    INSERT INTO minutes_fts(rowid, title, summary, transcript_text)
    VALUES (new.rowid, new.title, new.summary, new.transcript_text);
END;

CREATE TRIGGER IF NOT EXISTS minutes_ad AFTER DELETE ON minutes BEGIN
    INSERT INTO minutes_fts(minutes_fts, rowid, title, summary, transcript_text)
    VALUES ('delete', old.rowid, old.title, old.summary, old.transcript_text);
END;

CREATE TRIGGER IF NOT EXISTS minutes_au AFTER UPDATE ON minutes BEGIN
    INSERT INTO minutes_fts(minutes_fts, rowid, title, summary, transcript_text)
    VALUES ('delete', old.rowid, old.title, old.summary, old.transcript_text);
    INSERT INTO minutes_fts(rowid, title, summary, transcript_text)
    VALUES (new.rowid, new.title, new.summary, new.transcript_text);
END;
"""


def build_transcript_text(transcript: list[dict]) -> str:
    return "\n".join(seg["text"] for seg in transcript if seg.get("text"))


class Database:
    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path or DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        conn.executescript(FTS_SQL)
        conn.executescript(TRIGGERS_SQL)
        conn.commit()
        logger.info("Database initialized: %s", self._path)

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-20000")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.execute("PRAGMA mmap_size=67108864")
        return self._conn

    def insert_minutes(self, data: dict) -> None:
        conn = self._get_conn()
        if "transcript" in data and isinstance(data["transcript"], list):
            data["transcript_text"] = build_transcript_text(data["transcript"])
            data["transcript"] = json.dumps(data["transcript"], ensure_ascii=False)

        cols = ", ".join(data.keys())
        placeholders = ", ".join(f":{k}" for k in data.keys())
        conn.execute(f"INSERT INTO minutes ({cols}) VALUES ({placeholders})", data)
        conn.commit()

    def get_minutes(self, minutes_id: str, *, include_transcript: bool = True) -> dict | None:
        conn = self._get_conn()
        columns = "*" if include_transcript else (
            "id, session_id, project_id, title, date, started_at, duration_sec, summary, "
            "created_at, updated_at"
        )
        row = conn.execute(
            f"SELECT {columns} FROM minutes WHERE id = ?", (minutes_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def get_transcript(self, minutes_id: str) -> list[dict] | None:
        row = self._get_conn().execute(
            "SELECT transcript FROM minutes WHERE id = ?", (minutes_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            transcript = json.loads(row["transcript"])
        except (json.JSONDecodeError, TypeError):
            return []
        return transcript if isinstance(transcript, list) else []

    def has_minutes_for_session(self, session_id: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM minutes WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        return row is not None

    def get_minutes_by_session(self, session_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM minutes WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    # 一覧表示で必要な列のみ。transcript / transcript_text / context_snapshot は重いので除外。
    # summary は preview 表示のため先頭 240 字だけに切り出す (markdown summary は長くなりがち)。
    _LIST_COLUMNS: tuple[str, ...] = (
        "id", "session_id", "project_id", "title", "date", "started_at",
        "duration_sec", "summary", "created_at", "updated_at",
        "llm_model",
    )

    @classmethod
    def _list_columns_sql(cls, alias: str = "") -> str:
        """SELECT 句用の列リストを構築する。

        - alias を指定するとテーブルエイリアスを各カラムに前置する (例: "m.id")
        - summary は preview 用に先頭 240 字だけに切り出す
        - JOIN クエリでカラム名が衝突するケースに対応するため expression も
          エイリアスを明示する。
        """
        a = f"{alias}." if alias else ""
        parts: list[str] = []
        for c in cls._LIST_COLUMNS:
            if c == "summary":
                parts.append(f"substr({a}summary, 1, 240) AS summary")
            else:
                parts.append(f"{a}{c}")
        return ", ".join(parts)

    def list_minutes(self, project_id: str | None = None,
                     limit: int = 20, offset: int = 0) -> list[dict]:
        conn = self._get_conn()
        cols = self._list_columns_sql()
        if project_id:
            rows = conn.execute(
                f"SELECT {cols} FROM minutes WHERE project_id = ? "
                "ORDER BY date DESC, started_at DESC LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM minutes "
                "ORDER BY date DESC, started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            # フロントの Minutes 型互換のため空配列で埋める (詳細画面で再取得)
            d["transcript"] = []
            out.append(d)
        return out

    def count_minutes(self, project_id: str) -> int:
        row = self._get_conn().execute(
            "SELECT COUNT(*) FROM minutes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return row[0]

    def update_transcript(
        self,
        minutes_id: str,
        transcript: list[dict],
        *,
        clear_summary: bool = False,
    ) -> bool:
        conn = self._get_conn()
        transcript_text = build_transcript_text(transcript)
        transcript_json = json.dumps(transcript, ensure_ascii=False)
        if clear_summary:
            result = conn.execute(
                "UPDATE minutes SET transcript = ?, transcript_text = ?, summary = '', "
                "llm_model = '', context_snapshot = NULL, updated_at = datetime('now') WHERE id = ?",
                (transcript_json, transcript_text, minutes_id),
            )
        else:
            result = conn.execute(
                "UPDATE minutes SET transcript = ?, transcript_text = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (transcript_json, transcript_text, minutes_id),
            )
        conn.commit()
        return result.rowcount > 0

    def update_title(self, minutes_id: str, title: str) -> bool:
        conn = self._get_conn()
        result = conn.execute(
            "UPDATE minutes SET title = ?, updated_at = datetime('now') WHERE id = ?",
            (title, minutes_id),
        )
        conn.commit()
        return result.rowcount > 0

    def summary_context(self, minutes_id: str) -> dict:
        row = self._get_conn().execute(
            "SELECT context_snapshot FROM minutes WHERE id = ?", (minutes_id,),
        ).fetchone()
        try:
            context = json.loads(row["context_snapshot"] or "{}") if row else {}
        except (ValueError, TypeError):
            return {}
        return context if isinstance(context, dict) else {}

    def update_summary(
        self, minutes_id: str, summary: str, llm_model: str | None = None,
        *, context: dict | None = None,
    ) -> bool:
        conn = self._get_conn()
        snapshot = json.dumps(context, ensure_ascii=False) if context is not None else None
        result = conn.execute(
            "UPDATE minutes SET summary = ?, llm_model = COALESCE(?, llm_model), "
            "context_snapshot = COALESCE(?, context_snapshot), updated_at = datetime('now') WHERE id = ?",
            (summary, llm_model, snapshot, minutes_id),
        )
        conn.commit()
        return result.rowcount > 0

    def update_project(self, minutes_id: str, project_id: str) -> bool:
        conn = self._get_conn()
        result = conn.execute(
            "UPDATE minutes SET project_id = ?, updated_at = datetime('now') WHERE id = ?",
            (project_id, minutes_id),
        )
        conn.commit()
        return result.rowcount > 0

    def delete_minutes(self, minutes_id: str) -> bool:
        conn = self._get_conn()
        with conn:
            for table in ("meeting_notes", "meeting_chats"):
                conn.execute(
                    f"DELETE FROM {table} WHERE session_id = (SELECT session_id FROM minutes WHERE id = ?)",
                    (minutes_id,),
                )
            result = conn.execute("DELETE FROM minutes WHERE id = ?", (minutes_id,))
        return result.rowcount > 0

    def meeting_notes(self, session_id: str) -> list[dict]:
        rows = self._get_conn().execute(
            "SELECT * FROM meeting_notes WHERE session_id = ? ORDER BY start", (session_id,),
        )
        return [dict(row) for row in rows]

    def add_meeting_note(self, session_id: str, start: float, body: str, title: str | None) -> int | None:
        conn = self._get_conn()
        with conn:
            result = conn.execute(
                "INSERT INTO meeting_notes (session_id, start, number, body, title) "
                "SELECT ?, ?, COALESCE(MAX(number), 0) + 1, ?, ? FROM meeting_notes WHERE session_id = ? "
                "ON CONFLICT(session_id, start) DO NOTHING",
                (session_id, start, body, title, session_id),
            )
        return result.lastrowid if result.rowcount else None

    def edit_meeting_note(self, session_id: str, note_id: int, body: str, title: str | None) -> bool:
        conn = self._get_conn()
        with conn:
            result = conn.execute(
                "UPDATE meeting_notes SET body = ?, title = ?, "
                "auto_title = CASE WHEN body = ? AND title IS NULL THEN auto_title ELSE NULL END "
                "WHERE session_id = ? AND id = ?",
                (body, title, body, session_id, note_id),
            )
        return result.rowcount > 0

    def finish_note_title(self, session_id: str, note_id: int, body: str, title: str) -> bool:
        conn = self._get_conn()
        with conn:
            result = conn.execute(
                "UPDATE meeting_notes SET auto_title = ? "
                "WHERE session_id = ? AND id = ? AND body = ? AND title IS NULL AND auto_title IS NULL",
                (title, session_id, note_id, body),
            )
        return result.rowcount > 0

    def delete_meeting_note(self, session_id: str, note_id: int) -> bool:
        conn = self._get_conn()
        with conn:
            result = conn.execute("DELETE FROM meeting_notes WHERE session_id = ? AND id = ?", (session_id, note_id))
        return result.rowcount > 0

    def meeting_chats(self, session_id: str) -> list[dict]:
        rows = self._get_conn().execute(
            "SELECT id, title FROM meeting_chats WHERE session_id = ? ORDER BY updated_at DESC, rowid DESC",
            (session_id,),
        )
        return [dict(row) for row in rows]

    def meeting_chat(self, session_id: str, chat_id: str) -> dict | None:
        row = self._get_conn().execute(
            "SELECT id, title, messages FROM meeting_chats WHERE session_id = ? AND id = ?",
            (session_id, chat_id),
        ).fetchone()
        return {**dict(row), "messages": json.loads(row["messages"])} if row else None

    def create_meeting_chat(self, session_id: str, chat_id: str) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute("INSERT INTO meeting_chats (session_id, id) VALUES (?, ?)", (session_id, chat_id))

    def rename_meeting_chat(self, session_id: str, chat_id: str, title: str) -> bool:
        conn = self._get_conn()
        with conn:
            result = conn.execute(
                "UPDATE meeting_chats SET title = ? WHERE session_id = ? AND id = ?",
                (title, session_id, chat_id),
            )
        return result.rowcount > 0

    def finish_chat_turn(self, session_id: str, chat_id: str, messages: list[dict], title: str) -> bool:
        conn = self._get_conn()
        with conn:
            result = conn.execute(
                "UPDATE meeting_chats SET messages = ?, title = COALESCE(title, ?), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE session_id = ? AND id = ?",
                (json.dumps(messages, ensure_ascii=False), title, session_id, chat_id),
            )
        return result.rowcount > 0

    def delete_meeting_chat(self, session_id: str, chat_id: str) -> bool:
        conn = self._get_conn()
        with conn:
            result = conn.execute("DELETE FROM meeting_chats WHERE session_id = ? AND id = ?", (session_id, chat_id))
        return result.rowcount > 0

    def search(self, query: str, project_id: str | None = None,
               limit: int = 20) -> list[dict]:
        safe = self._sanitize_fts_query(query)
        if not safe:
            return []
        phrase = safe[1:-1]
        if len(phrase) < 3:
            # FTS5's trigram index cannot match a phrase shorter than three characters.
            return self._search_short_phrase(phrase, project_id, limit)

        conn = self._get_conn()
        # ハイライト用に  /  をマーカーとして埋め込み、
        # フロント側で <mark> に変換する。FTS5 の snippet() で前後文脈つき抜粋を生成。
        list_cols = self._list_columns_sql("m")
        cols = (
            f"{list_cols}, "
            "snippet(minutes_fts, 0, char(1), char(2), '…', 12) AS hl_title, "
            "snippet(minutes_fts, 1, char(1), char(2), '…', 20) AS hl_summary, "
            "snippet(minutes_fts, 2, char(1), char(2), '…', 24) AS hl_transcript"
        )
        if project_id:
            rows = conn.execute(
                f"""SELECT {cols} FROM minutes m
                   JOIN minutes_fts f ON m.rowid = f.rowid
                   WHERE minutes_fts MATCH ? AND m.project_id = ?
                   ORDER BY rank LIMIT ?""",
                (safe, project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT {cols} FROM minutes m
                   JOIN minutes_fts f ON m.rowid = f.rowid
                   WHERE minutes_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (safe, limit),
            ).fetchall()
        return [self._row_to_dict_with_highlights(r) for r in rows]

    def _search_short_phrase(self, phrase: str, project_id: str | None, limit: int) -> list[dict]:
        cols = self._list_columns_sql("m")
        scope = "AND m.project_id = ?" if project_id else ""
        args: list[str | int] = [phrase, phrase, phrase]
        if project_id:
            args.append(project_id)
        args.append(limit)
        rows = self._get_conn().execute(
            f"""SELECT {cols}, m.summary AS full_summary, m.transcript_text
                FROM minutes m
                WHERE (instr(lower(m.title), lower(?)) > 0
                    OR instr(lower(m.summary), lower(?)) > 0
                    OR instr(lower(m.transcript_text), lower(?)) > 0)
                {scope}
                ORDER BY m.date DESC, m.started_at DESC, m.rowid DESC
                LIMIT ?""",
            args,
        ).fetchall()
        pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        results: list[dict] = []
        for row in rows:
            data = dict(row)
            data["hl_title"] = self._short_highlight(data["title"], pattern)
            data["hl_summary"] = self._short_highlight(data.pop("full_summary"), pattern)
            data["hl_transcript"] = self._short_highlight(data.pop("transcript_text"), pattern)
            results.append(self._row_to_dict_with_highlights(data))
        return results

    @staticmethod
    def _short_highlight(text: str, pattern: re.Pattern[str]) -> str | None:
        match = pattern.search(text)
        if match is None:
            return None
        start = max(0, match.start() - 18)
        end = min(len(text), match.end() + 40)
        prefix = "…" if start else ""
        suffix = "…" if end < len(text) else ""
        return f"{prefix}{text[start:match.start()]}\x01{match[0]}\x02{text[match.end():end]}{suffix}"

    @staticmethod
    def _sanitize_fts_query(q: str) -> str:
        # FTS5 の構文文字を排除して安全な phrase クエリにする。
        cleaned = q.replace('"', '').replace("'", "").strip()
        if not cleaned:
            return ""
        return f'"{cleaned}"'

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        # Older databases may still have this column; keep its contents in SQLite but do not expose it.
        d.pop("whisper_model", None)
        d.pop("context_snapshot", None)
        if "transcript" in d and isinstance(d["transcript"], str):
            try:
                d["transcript"] = json.loads(d["transcript"])
            except json.JSONDecodeError:
                pass
        return d

    def _row_to_dict_with_highlights(self, row: sqlite3.Row | dict) -> dict:
        d = dict(row)
        hl = {
            "title": d.pop("hl_title", None),
            "summary": d.pop("hl_summary", None),
            "transcript": d.pop("hl_transcript", None),
        }
        # search も list と同じスリム構成 — transcript フルテキストは送らない
        d["transcript"] = []
        d["highlights"] = hl
        return d

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


db = Database()
