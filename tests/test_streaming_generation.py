from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.transcribe import streaming  # noqa: E402


class StreamingGenerationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_mlx_pkg = sys.modules.get("mlx_whisper")
        self._old_identify = streaming.speaker_memory.identify

    def tearDown(self) -> None:
        if self._old_mlx_pkg is not None:
            sys.modules["mlx_whisper"] = self._old_mlx_pkg
        else:
            sys.modules.pop("mlx_whisper", None)
        streaming.speaker_memory.identify = self._old_identify

    def _transcriber(self, emitted: list[dict]) -> streaming.StreamingTranscriber:
        transcriber = streaming.StreamingTranscriber(
            model_name="large-v3",
            language="ja",
            vad_provider="energy",
        )
        transcriber._repo = "fake-repo"
        transcriber._active_generation = 1
        transcriber._emit = emitted.append  # type: ignore[method-assign]
        return transcriber

    def test_stale_decode_result_is_not_applied(self) -> None:
        started = threading.Event()
        release = threading.Event()
        speaker_called = threading.Event()

        pkg = types.ModuleType("mlx_whisper")

        def fake_transcribe(_audio: np.ndarray, **_kwargs: object) -> dict:
            started.set()
            self.assertTrue(release.wait(timeout=1.0), "fake transcribe was not released")
            return {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 0.8,
                        "text": "古い結果",
                        "words": [{"start": 0.0, "end": 0.2, "word": "古い"}],
                    }
                ]
            }

        def fake_identify(*_args: object, **_kwargs: object) -> dict:
            speaker_called.set()
            return {"speaker_id": "speaker-1"}

        pkg.transcribe = fake_transcribe
        sys.modules["mlx_whisper"] = pkg
        streaming.speaker_memory.identify = fake_identify

        emitted: list[dict] = []
        transcriber = self._transcriber(emitted)
        audio = np.ones(streaming.SAMPLE_RATE, dtype=np.float32)

        worker = threading.Thread(
            target=transcriber._transcribe,
            args=(audio, 0.0, 1),
            daemon=True,
        )
        worker.start()

        self.assertTrue(started.wait(timeout=1.0), "fake transcribe did not start")
        transcriber._active_generation = 2
        release.set()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive(), "transcribe thread should finish")
        self.assertFalse(speaker_called.is_set(), "stale result should skip speaker identify")
        self.assertEqual([], transcriber.segments)
        self.assertEqual([], emitted)
        self.assertEqual([], transcriber._recent_ratios)
        self.assertEqual("", transcriber._recent_text_tail)

    def test_generation_change_after_speaker_identify_is_not_applied(self) -> None:
        pkg = types.ModuleType("mlx_whisper")

        def fake_transcribe(_audio: np.ndarray, **_kwargs: object) -> dict:
            return {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 0.7,
                        "text": "反映してはいけない結果",
                        "words": [],
                    }
                ]
            }

        pkg.transcribe = fake_transcribe
        sys.modules["mlx_whisper"] = pkg

        emitted: list[dict] = []
        transcriber = self._transcriber(emitted)

        def fake_identify(*_args: object, **_kwargs: object) -> dict:
            transcriber._active_generation = 2
            return {
                "speaker_id": "speaker-1",
                "speaker_label": "話者1",
                "speaker_confidence": 0.9,
            }

        streaming.speaker_memory.identify = fake_identify

        audio = np.ones(streaming.SAMPLE_RATE, dtype=np.float32)
        transcriber._transcribe(audio, 3.0, 1)

        self.assertEqual([], transcriber.segments)
        self.assertEqual([], emitted)
        self.assertEqual([], transcriber._recent_ratios)
        self.assertEqual("", transcriber._recent_text_tail)

    def test_restart_worker_cannot_advance_generation_during_final_apply(self) -> None:
        pkg = types.ModuleType("mlx_whisper")

        def fake_transcribe(_audio: np.ndarray, **_kwargs: object) -> dict:
            return {
                "segments": [
                    {
                        "start": 0.0,
                        "end": 0.7,
                        "text": "反映中の結果",
                        "words": [],
                    }
                ]
            }

        pkg.transcribe = fake_transcribe
        sys.modules["mlx_whisper"] = pkg
        streaming.speaker_memory.identify = lambda *_args, **_kwargs: {}

        emitted: list[dict] = []
        transcriber = self._transcriber(emitted)
        transcriber._running = True

        restart_done = threading.Event()
        restart_result: list[bool] = []
        observed_during_apply: dict[str, object] = {}

        def fake_run_worker(_generation: int) -> None:
            return

        transcriber._run_worker = fake_run_worker  # type: ignore[method-assign]

        class RestartingRatios(list[float]):
            def append(self, value: float) -> None:
                thread = threading.Thread(
                    target=lambda: (
                        restart_result.append(
                            transcriber.restart_worker("test final apply", force=True)
                        ),
                        restart_done.set(),
                    ),
                    daemon=True,
                )
                thread.start()
                observed_during_apply["restart_finished"] = restart_done.wait(timeout=0.05)
                observed_during_apply["generation"] = transcriber._active_generation
                observed_during_apply["thread"] = thread
                super().append(value)

        transcriber._recent_ratios = RestartingRatios()

        audio = np.ones(streaming.SAMPLE_RATE, dtype=np.float32)
        transcriber._transcribe(audio, 4.0, 1)

        restart_thread = observed_during_apply["thread"]
        self.assertIsInstance(restart_thread, threading.Thread)
        restart_thread.join(timeout=1.0)

        self.assertFalse(restart_thread.is_alive(), "restart thread should finish")
        self.assertEqual(False, observed_during_apply["restart_finished"])
        self.assertEqual(1, observed_during_apply["generation"])
        self.assertEqual([True], restart_result)
        self.assertEqual(2, transcriber._active_generation)
        self.assertEqual(1, len(transcriber.segments))
        self.assertEqual(1, len(emitted))

    def test_worker_exit_keeps_whisper_lease_until_cleanup(self) -> None:
        transcriber = self._transcriber([])
        transcriber._running = True
        transcriber._worker_count = 1
        transcriber._resource_lease = True
        self.assertTrue(streaming.model_resource_gate.try_acquire_whisper())

        transcriber._run_worker = lambda _generation: None  # type: ignore[method-assign]
        try:
            transcriber._run_worker_guarded(1)
            self.assertTrue(transcriber._resource_lease)
            self.assertEqual(1, streaming.model_resource_gate.snapshot()["whisper_users"])

            transcriber.cleanup()
            self.assertFalse(transcriber._resource_lease)
            self.assertEqual(0, streaming.model_resource_gate.snapshot()["whisper_users"])
        finally:
            if transcriber._resource_lease:
                transcriber.cleanup()


if __name__ == "__main__":
    unittest.main()
