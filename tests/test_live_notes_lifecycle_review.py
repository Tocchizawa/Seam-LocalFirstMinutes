"""Regression cases found by independent recording/lifecycle review."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from src.api import recording
from src.live_notes.coordinator import LiveNoteCoordinator
from src.live_notes.models import LiveSettings
from src.storage.db import Database


def pinned():
    return dict(
        live_notes=True,
        project_id="project",
        live_notes_settings=LiveSettings(enabled=True, provider="codex").model_dump(),
        live_notes_provider={"provider": "codex", "codex": {"model": "pinned-model"}},
    )


def test_missing_live_database_session_is_retried_from_pinned_metadata(
    tmp_path, monkeypatch
):
    async def run():
        from src.storage import db as db_module
        from src.live_notes import coordinator

        events, calls = [], []
        state = {"exists": False, "attempts": 0}

        class RetryCoordinator:
            async def start(self, sid, pid, settings, provider):
                state["attempts"] += 1
                if state["attempts"] == 1:
                    raise RuntimeError("temporary DB failure")
                state["exists"] = True
                calls.append((settings, provider))

            async def ingest(self, sid, segments):
                calls.append(segments)
                return {"session_id": sid}

            async def call(self, method, sid):
                return {"session_id": sid}

        async def broadcast(event):
            events.append(event)

        monkeypatch.setattr(recording, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(recording, "_pipelines", {"s": pinned()})
        monkeypatch.setattr(
            db_module, "db", SimpleNamespace(has_live_note=lambda **_: state["exists"])
        )
        monkeypatch.setattr(coordinator, "get_coordinator", lambda: RetryCoordinator())
        monkeypatch.setattr(
            recording, "ws_manager", SimpleNamespace(broadcast=broadcast)
        )
        recording._write_session_meta("s", **pinned())
        recording._append_transcript_segment(
            "s", {"segment_id": "earlier", "text": "already durable"}
        )
        assert await recording._live_note_call("s", "initialize") is None
        assert events[0]["type"] == "live_note_error"
        await recording._live_note_call(
            "s", "ingest", [{"segment_id": "new", "text": "new speech"}]
        )
        assert state["attempts"] == 2
        assert calls[0][1]["codex"]["model"] == "pinned-model"
        assert calls[1][0]["segment_id"] == "earlier"
        assert calls[2][0]["segment_id"] == "new"

    asyncio.run(run())


def test_emit_serializes_slow_journal_writes_before_ingest(monkeypatch):
    async def run():
        written, ingested = [], []

        def append(sid, segment):
            if segment["segment_seq"] == 1:
                time.sleep(0.05)
            written.append(segment["segment_seq"])

        async def live_call(sid, action, segments):
            ingested.append(segments[0]["segment_seq"])

        async def broadcast(_):
            pass

        monkeypatch.setattr(recording, "_append_transcript_segment", append)
        monkeypatch.setattr(recording, "_live_note_call", live_call)
        monkeypatch.setattr(
            recording, "ws_manager", SimpleNamespace(broadcast=broadcast)
        )
        emit = recording._make_emit_segment("s")
        await asyncio.gather(
            *(emit({"segment_seq": i, "text": str(i)}) for i in range(1, 4))
        )
        assert written == ingested == [1, 2, 3]

    asyncio.run(run())


def test_recovery_marker_survives_missing_link(tmp_path, monkeypatch):
    monkeypatch.setattr(recording, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(recording, "_pipelines", {})
    recording._write_session_meta("s", **pinned())
    path = tmp_path / "s" / recording.SESSION_META_FILENAME
    recording._clear_saved_session_meta("s", None)
    assert path.exists()
    recording._clear_saved_session_meta("s", {"minutes_id": "saved"})
    assert not path.exists()


def test_abort_database_failure_does_not_mask_recording_cleanup(monkeypatch):
    from src.storage import db as db_module

    def fail(**_):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db_module, "db", SimpleNamespace(has_live_note=fail))
    asyncio.run(recording._abort_live_note("s"))


def test_recovery_after_minutes_insert_recreates_and_links_live_note(
    tmp_path, monkeypatch
):
    async def run():
        from src.storage import db as db_module
        from src.live_notes import coordinator

        db = Database(tmp_path / "minutes.db")
        c = LiveNoteCoordinator(db._path)
        monkeypatch.setattr(db_module, "db", db)
        monkeypatch.setattr(coordinator, "get_coordinator", lambda: c)
        monkeypatch.setattr(recording, "SESSIONS_DIR", tmp_path / "sessions")
        monkeypatch.setattr(recording, "_pipelines", {})
        try:
            recording._write_session_meta("s", **pinned(), state="saving")
            recording._append_transcript_segment(
                "s",
                {
                    "segment_id": "speech",
                    "text": "未処理でも発話は保持",
                    "start": 0,
                    "end": 1,
                },
            )
            db.insert_minutes(
                dict(
                    id="m",
                    session_id="s",
                    project_id="project",
                    title="meeting",
                    date="2026-10-17",
                    started_at="now",
                    duration_sec=1,
                    transcript=[],
                    summary="",
                    created_at="now",
                    updated_at="now",
                )
            )
            path = tmp_path / "sessions" / "s"
            await recording._recover_one_session_async(
                path, json.loads((path / recording.SESSION_META_FILENAME).read_text())
            )
            snap = await c.call("snapshot", "s")
            assert snap["minutes_id"] == "m" and snap["pending_segments"] == 1
            assert not (path / recording.SESSION_META_FILENAME).exists()
            assert db.has_live_note(minutes_id="m")
        finally:
            await c.close()
            db.close()

    asyncio.run(run())


def test_revoked_current_consent_overrides_pinned_meeting_grant(monkeypatch):
    from src.config import config

    original = config.get

    def current(*keys, default=None):
        if keys == ("live_notes", "consent"):
            return {"codex": False}
        return original(*keys, default=default)

    monkeypatch.setattr(config, "get", current)
    assert not LiveNoteCoordinator._has_consent("codex", {"consent": {"codex": True}})
