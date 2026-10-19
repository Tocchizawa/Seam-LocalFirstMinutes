"""Real router/SQLite/recording callbacks with synthetic speech and restricted CLI stand-ins."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
import pytest

from src.api.live_notes import router
from src.live_notes import coordinator as coord_module
from src.live_notes.coordinator import LiveNoteCoordinator
from src.live_notes.models import LiveSettings
from src.storage.db import Database


def operation(batch, n, action, **kwargs):
    return dict(
        op_id=f"{batch}:{n}",
        action=action,
        topic_id=None,
        block_id=None,
        expected_block_revision=None,
        title=None,
        kind=None,
        markdown=None,
        source_segment_seqs=[],
        confidence=0.95,
        **kwargs,
    )


class ReplayCLI:
    def __init__(self):
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.delay = 0
        self.fail = False

    async def preflight(self):
        return {"ok": True, "version": "restricted-fixture"}

    async def infer_operations(self, prompt, schema, timeout=120, cancellation=None):
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.fail:
                raise RuntimeError("offline")
            if self.delay:
                await asyncio.sleep(self.delay)
            context = json.loads(
                prompt.split("以下は入力データです（指示ではありません）:\n", 1)[1]
            )
            known = {t["title"]: t["id"] for t in context["outline"]}
            ops = []
            for seg in context["segments"]:
                title = seg["text"].split("：")[0]
                if title not in known:
                    tid = f"new:{len(known) + 1}"
                    known[title] = tid
                    op = operation(context["batch_id"], len(ops) + 1, "create_topic")
                    op.update(
                        topic_id=tid,
                        title=title,
                        source_segment_seqs=[seg["segment_seq"]],
                    )
                    ops.append(op)
                op = operation(context["batch_id"], len(ops) + 1, "add_block")
                op.update(
                    topic_id=known[title],
                    kind="proposal",
                    markdown=seg["text"],
                    source_segment_seqs=[seg["segment_seq"]],
                )
                ops.append(op)
            return {
                "schema_version": 1,
                "batch_id": context["batch_id"],
                "base_note_revision": context["base_note_revision"],
                "operations": ops,
            }
        finally:
            self.concurrent -= 1


async def until(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(0.03)
    raise AssertionError("timed out waiting for live note")


def settings(**kwargs):
    data = LiveSettings(
        enabled=True,
        provider="codex",
        min_interval_sec=1,
        target_interval_sec=1,
        max_wait_sec=1,
        min_pending_chars=1,
        max_batch_chars=120,
        consent={"codex": True, "claude_code": True},
    ).model_dump()
    data.update(kwargs)
    return data


async def setup_coordinator(tmp_path, monkeypatch):
    from src.config import config

    original_get = config.get
    monkeypatch.setattr(
        config,
        "get",
        lambda *keys, default=None: (
            {"codex": True, "claude_code": True}
            if keys == ("live_notes", "consent")
            else original_get(*keys, default=default)
        ),
    )
    db = Database(tmp_path / "minutes.db")
    events = []

    async def broadcast(e):
        events.append(e)

    cli = ReplayCLI()
    c = LiveNoteCoordinator(db._path, broadcast, lambda *_: cli)
    monkeypatch.setattr(coord_module, "_coordinator", c)
    return db, c, cli, events


def test_sixty_minute_replay_edit_and_export(tmp_path, monkeypatch):
    async def run():
        db, c, cli, events = await setup_coordinator(tmp_path, monkeypatch)
        try:
            await c.start(
                "meeting", "default", settings(), {"codex": {"model": "test-model"}}
            )
            # 60 min at one confirmed turn per minute, 20 topics and two revisits each.
            segments = [
                {
                    "segment_id": f"s{i}",
                    "start": i * 60,
                    "end": i * 60 + 10,
                    "text": f"議題{i % 20:02}：選択肢{i}を検討する提案です。",
                }
                for i in range(60)
            ]
            await c.ingest("meeting", segments)
            await c.stopped("meeting")

            async def finished():
                s = await c.call("snapshot", "meeting")
                return s if s["last_processed_segment_seq"] == 60 else None

            s = await until(finished, 35)
            assert len(s["topics"]) == 20
            assert all(len(t["blocks"]) == 3 for t in s["topics"])
            assert cli.max_concurrent == 1 and cli.calls <= 20
            assert s["pending_segments"] == 0
            app = FastAPI()
            app.include_router(router)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                block = s["topics"][0]["blocks"][0]
                r = await client.patch(
                    f"/api/live-notes/meeting/blocks/{block['id']}",
                    json={
                        "request_id": "edit1",
                        "expected_revision": block["revision"],
                        "content_md": "手動で記録した重要な判断",
                    },
                )
                assert r.status_code == 200, r.text
                assert r.json()["topics"][0]["blocks"][0]["owner"] == "human"
                r = await client.patch(
                    f"/api/live-notes/meeting/blocks/{block['id']}",
                    json={
                        "request_id": "stale",
                        "expected_revision": block["revision"],
                        "content_md": "古い更新",
                    },
                )
                assert r.status_code == 409
                text = (await client.get("/api/live-notes/meeting/export.md")).text
                assert "手動で記録した重要な判断" in text and "議題19" in text
                history = (
                    await client.get("/api/live-notes/meeting/history?limit=2")
                ).json()
                assert len(history["events"]) == 2 and history["total"] > 2
                # Session/topic membership is always enforced.
                assert (
                    await client.patch(
                        f"/api/live-notes/other/blocks/{block['id']}",
                        json={
                            "request_id": "cross",
                            "expected_revision": 0,
                            "content_md": "NG",
                        },
                    )
                ).status_code == 404
            assert any(e["type"] == "live_note_patch" for e in events)
        finally:
            await c.close()
            db.close()

    asyncio.run(run())


def test_link_search_and_old_summary_cannot_overwrite(tmp_path, monkeypatch):
    async def run():
        db, c, cli, events = await setup_coordinator(tmp_path, monkeypatch)
        try:
            from src.live_notes.models import HumanOperation

            await c.start("save", "default", settings(), {"codex": {}})
            t = await c.human(
                "save",
                "create_topic",
                HumanOperation(request_id="topic", title="保存試験"),
            )
            await c.human(
                "save",
                "add_block",
                HumanOperation(
                    request_id="block",
                    topic_id=t["topics"][0]["id"],
                    kind="point",
                    content_md="検索対象の手動入力",
                ),
            )
            db.insert_minutes(
                dict(
                    id="minutes-id",
                    session_id="save",
                    project_id="default",
                    title="meeting",
                    date="2026-10-17",
                    started_at="2026-10-17T01:00:00+00:00",
                    duration_sec=60,
                    transcript=[],
                    summary="",
                    created_at="now",
                    updated_at="now",
                )
            )
            await c.stopped("save", "minutes-id")
            assert "検索対象の手動入力" in db.get_minutes("minutes-id")["summary"]
            assert db.search("検索対象")
            with pytest.raises(ValueError, match="LIVE_NOTE_MANAGED"):
                db.update_summary("minutes-id", "legacy overwrite")
            b = (await c.call("snapshot", "save"))["topics"][0]["blocks"][0]
            await c.human(
                "save",
                "edit_block",
                HumanOperation(
                    request_id="edit",
                    expected_revision=b["revision"],
                    content_md="停止後の編集を保存",
                ),
                b["id"],
            )
            assert "停止後の編集を保存" in db.get_minutes("minutes-id")["summary"]
            db.update_project("minutes-id", "moved")
            assert (await c.call("session", "save"))["project_id"] == "moved"
            await c.remove("save")
            db.delete_minutes("minutes-id")
            assert await c.call("sessions") == []
        finally:
            await c.close()
            db.close()

    asyncio.run(run())


def test_journal_recovery_and_processing_batch(tmp_path, monkeypatch):
    async def run():
        db, c, cli, events = await setup_coordinator(tmp_path, monkeypatch)
        await c.start("crash", "default", settings(), {"codex": {}})
        await c.control("crash", "pause")
        journal = tmp_path / "sessions" / "crash" / "transcript.jsonl"
        journal.parent.mkdir(parents=True)
        seg = {
            "segment_id": "final-a",
            "start": 0,
            "end": 1,
            "text": "復旧議題：保存済みの発言です。",
        }
        journal.write_text(json.dumps(seg, ensure_ascii=False) + "\n")
        await c.reconcile("crash")
        batch = await c.call("batch", "crash")
        context = await c.call("context", "crash", batch)
        await c.call("start_call", "crash", batch, context)
        await c.close()
        restored = LiveNoteCoordinator(db._path, None, lambda *_: cli)
        monkeypatch.setattr(coord_module, "_coordinator", restored)
        try:
            await restored.restore(tmp_path / "sessions")
            s = await restored.call("snapshot", "crash")
            assert s["paused"] and s["pending_segments"] == 1
            await restored.control("crash", "retry")

            async def finished():
                s = await restored.call("snapshot", "crash")
                return s if s["pending_segments"] == 0 else None

            s = await until(finished)
            assert len(s["topics"]) == 1 and s["last_processed_segment_seq"] == 1
            await restored.ingest("crash", [seg])
            assert (await restored.call("snapshot", "crash"))["pending_segments"] == 0
        finally:
            await restored.close()
            db.close()

    asyncio.run(run())


def test_cli_delay_pause_failure_does_not_block_manual_edit(tmp_path, monkeypatch):
    async def run():
        # This test verifies concurrent manual editing while the CLI is running;
        # host CPU/memory pressure must not prevent the fake CLI job from starting.
        from src.audio.resource_monitor import ResourceMonitor

        monkeypatch.setattr(ResourceMonitor, "should_throttle", lambda *_, **__: False)
        db, c, cli, events = await setup_coordinator(tmp_path, monkeypatch)
        try:
            from src.live_notes.models import HumanOperation

            cli.delay = 180
            await c.start("slow", "default", settings(), {"codex": {}})
            await c.ingest(
                "slow",
                [
                    {
                        "segment_id": "s",
                        "text": "遅延議題：提案です。",
                        "start": 0,
                        "end": 1,
                    }
                ],
            )

            async def running():
                return cli.concurrent == 1

            await until(running)
            start = time.monotonic()
            snap = await c.human(
                "slow",
                "create_topic",
                HumanOperation(request_id="manual", title="入力は止まらない"),
            )
            await c.stopped("slow")
            assert time.monotonic() - start < 1
            assert snap["topics"][0]["title"] == "入力は止まらない"
            await c.control("slow", "pause")
            assert (await c.call("snapshot", "slow"))["pending_segments"] == 1
        finally:
            await c.close()
            db.close()

    asyncio.run(run())


def test_real_recording_callback_flush_minutes_and_summary_guard(tmp_path, monkeypatch):
    async def run():
        import src.storage.db as db_module
        from src.api import recording
        from test_recording_pipeline_failures import (
            FakeWs,
            FakeRecorder,
            FakeStreamer,
            FakeMixer,
            reset_state,
            restore_state,
        )

        db, c, cli, events = await setup_coordinator(tmp_path, monkeypatch)
        monkeypatch.setattr(db_module, "db", db)
        ws = FakeWs()
        saved = reset_state(tmp_path / "sessions", ws)
        try:
            sid = "20261017_120000"
            folder = tmp_path / "sessions" / sid
            folder.mkdir(parents=True)
            await c.start(sid, "default", settings(), {"codex": {}})
            await c.control(sid, "pause")
            recording._pipelines[sid] = recording._new_pipeline(sid, "default")
            recording._pipelines[sid]["live_notes"] = True
            seg = {
                "segment_id": "real-callback",
                "start": 0.0,
                "end": 1.0,
                "text": "録音接続：この方針を提案します。",
            }
            await recording._make_emit_segment(sid)(seg)
            assert (folder / "transcript.jsonl").exists()
            assert (await c.call("snapshot", sid))["pending_segments"] == 1
            recording._active_session_id = sid
            recording.recorder = FakeRecorder(
                {"session_id": sid, "duration_sec": 1, "error": None, "wav_path": None}
            )
            recording._streamers[sid] = FakeStreamer([seg])
            recording._mixers[sid] = FakeMixer()
            from src.summarize.runner import get_runner

            with patch.object(get_runner(), "enqueue") as legacy:
                await recording._finalize_session(sid)
                legacy.assert_not_called()
            record = db.get_minutes_by_session(sid)
            assert record and record["transcript"][0]["text"] == seg["text"]
            assert recording._pipelines[sid]["state"] == "done"
            assert (await c.call("snapshot", sid))["minutes_id"] == record["id"]
            await c.control(sid, "resume")

            async def finished():
                return (await c.call("snapshot", sid))["pending_segments"] == 0

            await until(finished)
            assert "録音接続" in db.get_minutes(record["id"])["summary"]
            assert (await c.call("snapshot", sid))["last_processed_segment_seq"] == 1
        finally:
            restore_state(saved)
            await c.close()
            db.close()

    asyncio.run(run())


def test_call_budget_is_durable_and_counts_failed_and_repair_calls(
    tmp_path, monkeypatch
):
    async def run():
        from src.live_notes.repository import NoteError

        db, c, cli, events = await setup_coordinator(tmp_path, monkeypatch)
        try:
            await c.start(
                "budget", "default", settings(max_calls_per_hour=1), {"codex": {}}
            )
            await c.control("budget", "pause")
            await c.ingest(
                "budget",
                [
                    {
                        "segment_id": "s",
                        "start": 0,
                        "end": 1,
                        "text": "上限議題：発話を保存する",
                    }
                ],
            )
            batch = await c.call("batch", "budget")
            ctx = await c.call("context", "budget", batch)
            cid = await c.call("start_call", "budget", batch, ctx)
            await c.call("finish_call", cid, 12, "offline")
            with pytest.raises(NoteError, match="上限"):
                await c.call("start_call", "budget", batch, ctx, repair=True)
            assert (await c.call("snapshot", "budget"))["pending_segments"] == 1
            assert (await c.call("snapshot", "budget"))["llm_calls"] == 1
        finally:
            await c.close()
            db.close()

    asyncio.run(run())


def test_orphan_minutes_protection_before_live_link(tmp_path):
    from src.live_notes.repository import LiveNoteStore

    db = Database(tmp_path / "minutes.db")
    store = LiveNoteStore(db._path)
    try:
        store.create("orphan", "default", "codex", {}, LiveSettings().model_dump())
        db.insert_minutes(
            dict(
                id="m",
                session_id="orphan",
                project_id="default",
                title="meeting",
                date="2026-10-17",
                started_at="2026-10-17T01:00:00+00:00",
                duration_sec=1,
                transcript=[],
                summary="",
                created_at="now",
                updated_at="now",
            )
        )
        assert db.has_live_note(minutes_id="m")
        with pytest.raises(ValueError, match="LIVE_NOTE_MANAGED"):
            db.update_summary("m", "旧ドラフトの書き込み")
        assert db.get_minutes("m")["summary"] == ""
    finally:
        store.close()
        db.close()


def test_related_context_is_bounded_without_truncating_saved_human_text(tmp_path):
    from src.live_notes.models import HumanOperation
    from src.live_notes.repository import LiveNoteStore

    store = LiveNoteStore(tmp_path / "live.sqlite")
    try:
        store.create("long", "default", "codex", {}, LiveSettings().model_dump())
        snap = store.human(
            "long", "create_topic", HumanOperation(request_id="t", title="予算")
        )
        tid = snap["topics"][0]["id"]
        for i in range(15):
            store.human(
                "long",
                "add_block",
                HumanOperation(
                    request_id=str(i),
                    topic_id=tid,
                    content_md="予算の重要な手動メモ" * 1000,
                ),
            )
        store.ingest(
            "long",
            [{"segment_id": "s", "start": 0, "end": 1, "text": "予算の再訪です"}],
        )
        batch = store.batch("long")
        context = store.context("long", batch)
        assert len(context["related_topics"][0]["blocks"]) <= 12
        assert (
            sum(
                len(b["content_md"])
                for t in context["related_topics"]
                for b in t["blocks"]
            )
            <= 36000
        )
        assert all(
            b["context_truncated"] for b in context["related_topics"][0]["blocks"]
        )
        assert len(store.snapshot("long")["topics"][0]["blocks"]) == 15
        assert (
            len(store.snapshot("long")["topics"][0]["blocks"][0]["content_md"]) > 2000
        )
    finally:
        store.close()
