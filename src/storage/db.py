from __future__ import annotations

import json
import logging
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
    whisper_model   TEXT,
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

    def get_minutes(self, minutes_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM minutes WHERE id = ?", (minutes_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

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
        "whisper_model", "llm_model",
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

    def update_transcript(self, minutes_id: str, transcript: list[dict]) -> bool:
        conn = self._get_conn()
        transcript_text = build_transcript_text(transcript)
        transcript_json = json.dumps(transcript, ensure_ascii=False)
        result = conn.execute(
            "UPDATE minutes SET transcript = ?, transcript_text = ?, updated_at = datetime('now') WHERE id = ?",
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

    def update_summary(self, minutes_id: str, summary: str,
                       llm_model: str | None = None) -> bool:
        conn = self._get_conn()
        if llm_model is None:
            result = conn.execute(
                "UPDATE minutes SET summary = ?, updated_at = datetime('now') WHERE id = ?",
                (summary, minutes_id),
            )
        else:
            result = conn.execute(
                "UPDATE minutes SET summary = ?, llm_model = ?, updated_at = datetime('now') WHERE id = ?",
                (summary, llm_model, minutes_id),
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

    def reassign_speaker_id(
        self,
        source_id: str,
        target_id: str | None,
        target_label: str | None = None,
    ) -> int:
        """全議事録の transcript で speaker_id=source_id を target_id (or None) に書き換える。

        target_id=None の場合は speaker_id/label/confidence を null にする (= "話者?")。
        target_label を指定すると label も上書き。
        戻り値は更新されたセグメント数。
        """
        conn = self._get_conn()
        rows = conn.execute("SELECT id, transcript FROM minutes").fetchall()
        updated_segs = 0
        for row in rows:
            try:
                transcript = json.loads(row["transcript"])
            except Exception:
                continue
            if not isinstance(transcript, list):
                continue
            changed = False
            for seg in transcript:
                if not isinstance(seg, dict):
                    continue
                if str(seg.get("speaker_id") or "") != source_id:
                    continue
                if target_id is None:
                    seg["speaker_id"] = None
                    seg["speaker_label"] = None
                    seg["speaker_confidence"] = None
                else:
                    seg["speaker_id"] = target_id
                    if target_label is not None:
                        seg["speaker_label"] = target_label
                changed = True
                updated_segs += 1
            if changed:
                new_text = build_transcript_text(transcript)
                conn.execute(
                    "UPDATE minutes SET transcript = ?, transcript_text = ?, updated_at = datetime('now') WHERE id = ?",
                    (json.dumps(transcript, ensure_ascii=False), new_text, row["id"]),
                )
        conn.commit()
        return updated_segs

    def delete_minutes(self, minutes_id: str) -> bool:
        conn = self._get_conn()
        result = conn.execute("DELETE FROM minutes WHERE id = ?", (minutes_id,))
        conn.commit()
        return result.rowcount > 0

    def search(self, query: str, project_id: str | None = None,
               limit: int = 20) -> list[dict]:
        safe = self._sanitize_fts_query(query)
        if not safe:
            return []

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

    @staticmethod
    def _sanitize_fts_query(q: str) -> str:
        # FTS5 の構文文字を排除して安全な phrase クエリにする。
        cleaned = q.replace('"', '').replace("'", "").strip()
        if not cleaned:
            return ""
        return f'"{cleaned}"'

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        if "transcript" in d and isinstance(d["transcript"], str):
            try:
                d["transcript"] = json.loads(d["transcript"])
            except json.JSONDecodeError:
                pass
        return d

    def _row_to_dict_with_highlights(self, row: sqlite3.Row) -> dict:
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
