"""Recording pipeline failure handling tests."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api import recording


class FakeWs:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def broadcast(self, event: dict) -> None:
        self.events.append(event)


class FakeRecorder:
    def __init__(self, result: dict, *, error: str | None = None) -> None:
        self.result = result
        self.error = error
        self.stopped = False

    def stop(self) -> dict:
        self.stopped = True
        return dict(self.result)


class FakeStreamer:
    def __init__(self, segments: list[dict] | None = None) -> None:
        self.segments = segments or []
        self.model_error = None
        self.cleaned = False

    def flush(self) -> list[dict]:
        return list(self.segments)

    def cleanup(self) -> None:
        self.cleaned = True


class FakeMixer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeRealtimeMixer(FakeMixer):
    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__()
        self.started = False

    def start(self, *_args, **_kwargs) -> None:
        self.started = True

    def feed_mic(self, *_args, **_kwargs) -> None:
        pass

    def feed_system(self, *_args, **_kwargs) -> None:
        pass


class FakeStartStreamer:
    def __init__(self, *_args, **_kwargs) -> None:
        self.cleaned = False

    def start(self, *_args, **_kwargs) -> None:
        pass

    def feed(self, *_args, **_kwargs) -> None:
        pass

    def cleanup(self) -> None:
        self.cleaned = True


class FakeStartRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.stopped = False

    def start(self, **_kwargs) -> dict:
        self.is_recording = True
        return {
            "session_id": _kwargs.get("session_id"),
            "has_system_audio": False,
            "system_error": "Core Audio Tap sidecar failed",
        }

    def stop(self) -> dict:
        self.stopped = True
        self.is_recording = False
        return {"session_id": "stopped"}


class FailingDb:
    def insert_minutes(self, _data: dict) -> None:
        raise RuntimeError("database is locked")

    def get_minutes(self, _minutes_id: str) -> dict | None:
        return None


class CapturingDb:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def insert_minutes(self, data: dict) -> None:
        self.records[str(data["id"])] = dict(data)

    def get_minutes(self, minutes_id: str) -> dict | None:
        record = self.records.get(str(minutes_id))
        return dict(record) if record is not None else None


def reset_state(tmp: Path, ws: FakeWs) -> dict:
    saved = {
        "SESSIONS_DIR": recording.SESSIONS_DIR,
        "recorder": recording.recorder,
        "speaker_memory": recording.speaker_memory,
        "ws_manager": recording.ws_manager,
        "_active_session_id": recording._active_session_id,
        "_last_result": recording._last_result,
    }
    recording.SESSIONS_DIR = tmp
    recording.recorder = None  # type: ignore[assignment]
    recording.speaker_memory = types.SimpleNamespace(
        rediarize_segments=lambda segments, **_kwargs: list(segments),
    )
    recording.ws_manager = ws
    recording._pipelines.clear()
    recording._streamers.clear()
    recording._mixers.clear()
    recording._active_session_id = None
    recording._last_result = None
    return saved


def restore_state(saved: dict) -> None:
    recording.SESSIONS_DIR = saved["SESSIONS_DIR"]
    recording.recorder = saved["recorder"]
    recording.speaker_memory = saved["speaker_memory"]
    recording.ws_manager = saved["ws_manager"]
    recording._active_session_id = saved["_active_session_id"]
    recording._last_result = saved["_last_result"]
    recording._pipelines.clear()
    recording._streamers.clear()
    recording._mixers.clear()


async def _run_db_save_failure_is_not_done() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = FakeWs()
        saved = reset_state(Path(td), ws)
        old_db_module = sys.modules.get("src.storage.db")
        sys.modules["src.storage.db"] = types.SimpleNamespace(db=FailingDb())  # type: ignore[assignment]
        try:
            sid = "20260622_120000"
            recording._pipelines[sid] = recording._new_pipeline(sid, "project-a")
            recording._active_session_id = sid
            recording.recorder = FakeRecorder({  # type: ignore[assignment]
                "session_id": sid,
                "session_dir": str(Path(td) / sid),
                "wav_path": None,
                "duration_sec": 1,
                "error": None,
            })
            recording._streamers[sid] = FakeStreamer([
                {"start": 0.0, "end": 1.0, "text": "hello"},
            ])  # type: ignore[assignment]
            recording._mixers[sid] = FakeMixer()  # type: ignore[assignment]

            await recording._finalize_session(sid)

            event_types = [e.get("type") for e in ws.events]
            pipeline = recording._pipelines[sid]
            meta = json.loads(
                (Path(td) / sid / recording.SESSION_META_FILENAME).read_text(encoding="utf-8"),
            )
            assert pipeline.get("state") == "error"
            assert "pipeline_done" not in event_types
            assert "pipeline_error" in event_types
            assert meta.get("state") == "saving"
        finally:
            if old_db_module is None:
                sys.modules.pop("src.storage.db", None)
            else:
                sys.modules["src.storage.db"] = old_db_module
            restore_state(saved)


async def _run_mic_failure_releases_active_recording() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = FakeWs()
        saved = reset_state(Path(td), ws)
        try:
            sid = "20260622_121500"
            recording._pipelines[sid] = recording._new_pipeline(sid, "project-a")
            recording._active_session_id = sid
            fake_recorder = FakeRecorder({
                "session_id": sid,
                "session_dir": str(Path(td) / sid),
                "wav_path": None,
                "duration_sec": 2,
                "error": "mic stream dead",
            }, error="mic stream dead")
            fake_streamer = FakeStreamer()
            fake_mixer = FakeMixer()
            recording.recorder = fake_recorder  # type: ignore[assignment]
            recording._streamers[sid] = fake_streamer  # type: ignore[assignment]
            recording._mixers[sid] = fake_mixer  # type: ignore[assignment]

            await recording._stop_failed_active_recording(sid, "mic stream dead")

            event_types = [e.get("type") for e in ws.events]
            meta = json.loads(
                (Path(td) / sid / recording.SESSION_META_FILENAME).read_text(encoding="utf-8"),
            )
            assert fake_recorder.stopped
            assert recording._active_session_id is None
            assert recording._pipelines[sid].get("state") == "error"
            assert "recording_stopped" in event_types
            assert "pipeline_error" in event_types
            assert fake_streamer.cleaned
            assert fake_mixer.stopped
            assert meta.get("state") == "stopping"
        finally:
            restore_state(saved)


async def _run_system_audio_failure_is_not_done() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = FakeWs()
        saved = reset_state(Path(td), ws)
        try:
            sid = "20260622_122000"
            session_dir = Path(td) / sid
            session_dir.mkdir()
            combined = session_dir / "combined.flac"
            combined.write_bytes(b"0" * 128)
            recording._pipelines[sid] = recording._new_pipeline(
                sid,
                "project-a",
                capture_system=True,
            )
            recording._active_session_id = sid
            recording.recorder = FakeRecorder({  # type: ignore[assignment]
                "session_id": sid,
                "session_dir": str(session_dir),
                "wav_path": str(combined),
                "combined_wav": str(combined),
                "duration_sec": 3,
                "error": "System audio conversion failed: ffmpeg exited 1",
            })
            recording._streamers[sid] = FakeStreamer([
                {"start": 0.0, "end": 1.0, "text": "mic only"},
            ])  # type: ignore[assignment]
            recording._mixers[sid] = FakeMixer()  # type: ignore[assignment]

            await recording._finalize_session(sid)

            event_types = [e.get("type") for e in ws.events]
            pipeline = recording._pipelines[sid]
            assert pipeline.get("state") == "error"
            assert "内部音声" in str(pipeline.get("error"))
            assert "pipeline_done" not in event_types
            assert "pipeline_error" in event_types
        finally:
            restore_state(saved)


async def _run_system_audio_start_failure_does_not_record() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = FakeWs()
        saved = reset_state(Path(td), ws)
        old_streamer = recording.StreamingTranscriber
        old_mixer = recording.RealtimeMixer
        old_project_manager = recording.project_manager
        try:
            fake_recorder = FakeStartRecorder()
            recording.recorder = fake_recorder  # type: ignore[assignment]
            recording.StreamingTranscriber = FakeStartStreamer  # type: ignore[assignment]
            recording.RealtimeMixer = FakeRealtimeMixer  # type: ignore[assignment]
            recording.project_manager = types.SimpleNamespace(get=lambda _project_id: None)

            try:
                await recording.start_recording(
                    recording.StartRequest(project_id="project-a", capture_system=True),
                )
                raised = False
            except Exception as e:
                raised = getattr(e, "status_code", None) == 409
                detail = getattr(e, "detail", {})
                assert detail.get("code") == "SYSTEM_AUDIO_START_FAILED"

            assert raised
            assert fake_recorder.stopped
            assert recording._active_session_id is None
            assert not recording._pipelines
            assert not recording._streamers
            assert not recording._mixers
        finally:
            recording.StreamingTranscriber = old_streamer
            recording.RealtimeMixer = old_mixer
            recording.project_manager = old_project_manager
            restore_state(saved)


async def _run_combined_audio_is_final_transcript_source() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = FakeWs()
        saved = reset_state(Path(td), ws)
        old_db_module = sys.modules.get("src.storage.db")
        old_minutes_module = sys.modules.get("src.api.minutes")
        fake_db = CapturingDb()
        retranscribe_calls: list[dict] = []

        async def fake_start_retranscribe(minutes_id: str, *, enqueue_summary_on_complete: bool = False) -> dict:
            retranscribe_calls.append({
                "minutes_id": minutes_id,
                "enqueue_summary_on_complete": enqueue_summary_on_complete,
            })
            return {"status": "started", "session_id": sid}

        sys.modules["src.storage.db"] = types.SimpleNamespace(db=fake_db)  # type: ignore[assignment]
        sys.modules["src.api.minutes"] = types.SimpleNamespace(  # type: ignore[assignment]
            _start_retranscribe_minutes=fake_start_retranscribe,
        )
        try:
            sid = "20260622_123000"
            session_dir = Path(td) / sid
            session_dir.mkdir()
            combined = session_dir / "combined.flac"
            combined.write_bytes(b"0" * 128)
            recording._pipelines[sid] = recording._new_pipeline(sid, "project-a")
            recording._active_session_id = sid
            recording.recorder = FakeRecorder({  # type: ignore[assignment]
                "session_id": sid,
                "session_dir": str(session_dir),
                "wav_path": str(combined),
                "combined_wav": str(combined),
                "duration_sec": 3,
                "error": None,
            })
            recording._streamers[sid] = FakeStreamer([
                {"start": 0.0, "end": 1.0, "text": "live transcript"},
            ])  # type: ignore[assignment]
            recording._mixers[sid] = FakeMixer()  # type: ignore[assignment]

            await recording._finalize_session(sid)

            assert len(fake_db.records) == 1
            saved_minutes = next(iter(fake_db.records.values()))
            assert saved_minutes["transcript"] == []
            assert len(retranscribe_calls) == 1
            assert retranscribe_calls[0]["minutes_id"] == saved_minutes["id"]
            assert retranscribe_calls[0]["enqueue_summary_on_complete"] is True
            assert recording._pipelines[sid].get("state") == "done"
        finally:
            if old_db_module is None:
                sys.modules.pop("src.storage.db", None)
            else:
                sys.modules["src.storage.db"] = old_db_module
            if old_minutes_module is None:
                sys.modules.pop("src.api.minutes", None)
            else:
                sys.modules["src.api.minutes"] = old_minutes_module
            restore_state(saved)


def test_db_save_failure_is_not_done() -> None:
    asyncio.run(_run_db_save_failure_is_not_done())


def test_mic_failure_releases_active_recording() -> None:
    asyncio.run(_run_mic_failure_releases_active_recording())


def test_system_audio_failure_is_not_done() -> None:
    asyncio.run(_run_system_audio_failure_is_not_done())


def test_system_audio_start_failure_does_not_record() -> None:
    asyncio.run(_run_system_audio_start_failure_does_not_record())


def test_combined_audio_is_final_transcript_source() -> None:
    asyncio.run(_run_combined_audio_is_final_transcript_source())


def test_combined_final_transcript_required_with_combined_audio() -> None:
    with tempfile.TemporaryDirectory() as td:
        combined = Path(td) / "combined.flac"
        combined.write_bytes(b"0" * 128)
        assert recording._should_finalize_from_combined({
            "combined_wav": str(combined),
            "mic_overflow_total": 0,
            "mic_padding_sec": 0.0,
        })


def test_combined_final_transcript_not_required_without_combined_audio() -> None:
    assert not recording._should_finalize_from_combined({
        "mic_wav": "/tmp/session/mic.wav",
        "mic_overflow_total": 10,
        "mic_padding_sec": 2.0,
    })


def test_combined_final_transcript_not_required_for_empty_combined_audio() -> None:
    with tempfile.TemporaryDirectory() as td:
        combined = Path(td) / "combined.flac"
        combined.write_bytes(b"")
        assert not recording._should_finalize_from_combined({
            "combined_wav": str(combined),
        })


def _run_as_script(tests: list[Callable[[], None]]) -> int:
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL: {test.__name__}: {e}")
    print("\n=========================================")
    print(f"  結果: {len(tests) - failed} passed, {failed} failed")
    print("=========================================")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_as_script([
        test_db_save_failure_is_not_done,
        test_mic_failure_releases_active_recording,
        test_system_audio_failure_is_not_done,
        test_system_audio_start_failure_does_not_record,
        test_combined_audio_is_final_transcript_source,
        test_combined_final_transcript_required_with_combined_audio,
        test_combined_final_transcript_not_required_without_combined_audio,
        test_combined_final_transcript_not_required_for_empty_combined_audio,
    ]))
