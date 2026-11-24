"""発言メモ、会議チャット、メモ変更後の要約更新の回帰テスト。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from src.api import meeting, recording
from src.storage import db as storage
from src.summarize import runner as summaries
from src.summarize.base import ProjectContext, ProviderHealth, SummaryError, SummaryErrorCode, SummaryResult

SESSION = "test_meeting"
SEGMENTS = [
    {"start": 1.25, "end": 6.0, "text": "会議の目的は次のリリースの準備を進めることです。"},
    {"start": 7.5, "end": 12.0, "text": "資料を確認して、金曜日までに次回の計画をまとめることになりました。"},
]


class FakeAI:
    name = "ollama"

    def __init__(self, *, resume=False):
        self.supports_resume = resume
        self.calls = []
        self.fail = False

    async def health_check(self):
        return ProviderHealth(True, "READY", "ready", "test-model")

    async def generate(self, transcript, **options):
        self.calls.append((transcript, options))
        if self.fail:
            raise SummaryError(SummaryErrorCode.PROVIDER_DOWN, "test failure")
        messages = options.get("messages") or []
        if "短いラベル名" in (messages[0]["content"] if messages else ""):
            answer = "次回の計画"
        elif messages and "会議の文字起こし" in messages[0]["content"]:
            answer = "金曜日までです [00:07](seam-transcript:7.5)"
        else:
            answer = f"## 概要\n\n要約 {len(self.calls)}"
        if callback := options.get("on_token"):
            emitted = callback(answer)
            if asyncio.iscoroutine(emitted):
                await emitted
        return SummaryResult(
            answer, self.name, "test-model", len(transcript), len(answer), 0,
            session_id="test-ai-session" if self.supports_resume else None,
        )


class MeetingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        self.db = storage.Database(self.root / "minutes.db")
        self.addCleanup(self.db.close)
        self.enterContext(patch.object(storage, "db", self.db))
        self.enterContext(patch.object(meeting, "db", self.db))
        self.enterContext(patch.object(summaries, "SUMMARY_QUEUE_PATH", self.root / "queue.json"))
        self.enterContext(patch.object(summaries, "DRAFTS_DIR", self.root / "drafts"))

    def save_meeting(self):
        self.db.insert_minutes({
            "id": "minute", "session_id": SESSION, "project_id": "project", "title": "会議",
            "date": "2026-11-24", "started_at": "2026-11-24T01:00:00Z", "duration_sec": 13,
            "transcript": SEGMENTS, "summary": "", "created_at": "", "updated_at": "",
        })

    async def test_live_notes_and_chat_follow_the_recording_into_the_archive(self):
        provider = FakeAI()
        self.enterContext(patch.object(meeting, "get_provider", return_value=provider))
        self.enterContext(patch.object(meeting, "get_runner", return_value=summaries.SummaryRunner()))
        self.enterContext(patch.object(recording, "SESSIONS_DIR", self.root))
        self.enterContext(patch.dict(recording._pipelines, {
            SESSION: {
                "state": "recording", "project_id": "project",
                "transcript_preview": {"start": 12.5, "text": "いま話している進捗"},
            },
        }))
        (self.root / SESSION).mkdir()
        (self.root / SESSION / recording.TRANSCRIPT_JSONL_FILENAME).write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in SEGMENTS), encoding="utf-8",
        )
        (self.root / "agenda.md").write_text("プロジェクトの参考資料", encoding="utf-8")
        self.enterContext(patch.object(meeting, "build_project_context", return_value=ProjectContext(
            name="会議プロジェクト", doc_dirs=[str(self.root)],
        )))

        app = FastAPI()
        app.include_router(meeting.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = f"/api/meeting/{SESSION}"
            memo = {"start": 7.5, "body": "金曜の計画について確認する"}
            created = await client.post(path + "/notes", json=memo)
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["notes"][0]["title"], "ラベル1")
            self.assertEqual((await client.get(path + "/notes")).json()[0]["title"], "次回の計画")
            self.assertEqual((await client.post(path + "/notes", json=memo)).status_code, 409)

            chat = (await client.post(path + "/chats")).json()
            chat_path = path + "/chats/" + chat["id"]
            answer = await client.post(chat_path + "/messages", json={"content": "今どうなってる？"})
            events = [json.loads(line) for line in answer.text.splitlines()]
            self.assertEqual([event["type"] for event in events], ["chunk", "done"])
            self.assertIn("seam-transcript:7.5", events[-1]["chat"]["messages"][-1]["content"])
            context = provider.calls[-1][1]["messages"][0]["content"]
            for expected in ["いま話している進捗", "金曜の計画について確認する", "プロジェクトの参考資料"]:
                self.assertIn(expected, context)

            self.save_meeting()
            recording._pipelines.pop(SESSION)
            self.assertEqual((await client.get(chat_path)).json()["title"], "今どうなってる？")
            renamed = await client.put(chat_path, json={"title": "進捗の確認"})
            self.assertEqual(renamed.json()["title"], "進捗の確認")
            follow_up = await client.post(chat_path + "/messages", json={"content": "期限は？", "focus_start": 7.5})
            self.assertEqual(follow_up.status_code, 200)
            messages = provider.calls[-1][1]["messages"]
            self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "user"])
            self.assertIn("質問の対象は [7.5]", messages[0]["content"])
            self.assertEqual(len((await client.get(chat_path)).json()["messages"]), 4)
            invalid_focus = await client.post(chat_path + "/messages", json={"content": "何の話？", "focus_start": 99})
            self.assertEqual(invalid_focus.status_code, 404)

            self.db.delete_minutes("minute")
            self.assertEqual(self.db.meeting_notes(SESSION), [])
            self.assertEqual(self.db.meeting_chats(SESSION), [])

    def test_chat_context_keeps_earlier_evidence_and_checks_reference_times(self):
        segments = [
            {"start": float(index), "text": "新しい話題について話しています。" * 4}
            for index in range(200)
        ]
        segments[12]["text"] = "商用トークンの失効日は3月末と合意しました。"
        transcript, excerpted = meeting._transcript_for_question(segments, "トークンの失効日は？", 1800)
        self.assertTrue(excerpted)
        self.assertIn("[12.0] 商用トークン", transcript)
        self.assertIn("[199.0]", transcript)
        answer = "回答 ［9:00］(seam-transcript:12.0) [99:00](seam-transcript:6000)"
        self.assertEqual(meeting._checked_citations(answer, segments), "回答 [00:12](seam-transcript:12.0)")

    async def test_summary_refresh_preserves_context_for_resumable_and_stateless_ai(self):
        self.save_meeting()
        cfg = {"provider": "ollama", "generate_title": False, "consent": {"openai": True}}
        self.enterContext(patch.object(summaries.config, "get", side_effect=lambda key, *args, **kwargs: cfg))
        for provider_name, resume in [("codex", True), ("openai", False)]:
            with self.subTest(provider=provider_name):
                ai = FakeAI(resume=resume)
                ai.name = provider_name
                worker = summaries.SummaryRunner()
                self.db.update_summary("minute", "", context={})
                with patch.object(summaries, "get_provider", return_value=ai) as factory:
                    with patch.object(worker, "_schedule_phase_b"):
                        await worker.start()
                        try:
                            self.assertTrue(worker.enqueue("minute", provider_name=provider_name))
                            await self.wait_for(worker, "done")
                            original = self.db.get_minutes("minute")["summary"]

                            note_id = self.db.add_meeting_note(SESSION, 7.5, "期限を追記", "確認")
                            self.assertTrue(worker.refresh_for_notes(SESSION))
                            await self.wait_for(worker, "done")
                            refresh = ai.calls[-1][1]
                            self.assertEqual(refresh["messages"][2]["content"], original)
                            self.assertIn("期限を追記", refresh["messages"][-1]["content"])
                            self.assertEqual(refresh.get("resume_session"), "test-ai-session" if resume else None)
                            self.assertEqual(factory.call_args.args[0], provider_name)

                            current = self.db.get_minutes("minute")["summary"]
                            ai.fail = True
                            self.db.delete_meeting_note(SESSION, note_id)
                            self.assertTrue(worker.refresh_for_notes(SESSION))
                            await self.wait_for(worker, "failed")
                            self.assertEqual(self.db.get_minutes("minute")["summary"], current)
                        finally:
                            await worker.shutdown()

    async def wait_for(self, worker, state):
        async with asyncio.timeout(3):
            while worker.get_status("minute").state != state:
                await asyncio.sleep(.01)
