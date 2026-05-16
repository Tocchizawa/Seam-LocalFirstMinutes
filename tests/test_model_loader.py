from __future__ import annotations

import sys
import threading
import time
import types
import unittest

from src.transcribe import streaming


class ModelLoaderCoordinationTest(unittest.TestCase):
    def setUp(self) -> None:
        streaming._loaded_repo = None
        streaming._loading_repo = None
        streaming._load_error = None
        streaming._load_event.clear()
        sys.modules.pop("mlx_whisper", None)
        sys.modules.pop("mlx_whisper.load_models", None)

    def tearDown(self) -> None:
        sys.modules.pop("mlx_whisper", None)
        sys.modules.pop("mlx_whisper.load_models", None)

    def _install_fake_loader(self, fn) -> None:
        root = types.ModuleType("mlx_whisper")
        load_mod = types.ModuleType("mlx_whisper.load_models")
        load_mod.load_model = fn
        root.load_models = load_mod
        sys.modules["mlx_whisper"] = root
        sys.modules["mlx_whisper.load_models"] = load_mod

    def test_parallel_callers_only_load_once(self) -> None:
        calls: list[str] = []

        def fake_load(repo: str) -> None:
            calls.append(repo)
            time.sleep(0.15)

        self._install_fake_loader(fake_load)

        results: list[str] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(streaming.get_or_load_model("medium", timeout_sec=2.0))
            except Exception as e:  # pragma: no cover - failure path capture
                errors.append(e)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(calls, ["mlx-community/whisper-medium-mlx"])

    def test_wait_timeout_if_another_thread_is_stuck_loading(self) -> None:
        gate = threading.Event()

        def fake_load(_repo: str) -> None:
            gate.wait(timeout=5.0)

        self._install_fake_loader(fake_load)

        loader_thread = threading.Thread(
            target=lambda: streaming.get_or_load_model("medium", timeout_sec=5.0),
            daemon=True,
        )
        loader_thread.start()

        deadline = time.time() + 1.0
        while streaming._loading_repo is None and time.time() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(streaming._loading_repo)

        with self.assertRaises(TimeoutError):
            streaming.get_or_load_model("medium", timeout_sec=0.2)

        gate.set()
        loader_thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
