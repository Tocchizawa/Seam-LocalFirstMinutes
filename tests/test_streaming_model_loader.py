from __future__ import annotations

import sys
import threading
import time
import types
import unittest

from src.transcribe import streaming


class StreamingModelLoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        streaming._loaded_repo = None
        streaming._loading_repo = None
        streaming._loading_started_at = None
        streaming._loading_token = 0
        streaming._load_error = None
        streaming._load_event.clear()

        self._old_mlx_pkg = sys.modules.get("mlx_whisper")
        self._old_mlx_load = sys.modules.get("mlx_whisper.load_models")

    def tearDown(self) -> None:
        if self._old_mlx_pkg is not None:
            sys.modules["mlx_whisper"] = self._old_mlx_pkg
        else:
            sys.modules.pop("mlx_whisper", None)
        if self._old_mlx_load is not None:
            sys.modules["mlx_whisper.load_models"] = self._old_mlx_load
        else:
            sys.modules.pop("mlx_whisper.load_models", None)

    def test_stale_loader_lease_can_be_expired_and_retried(self) -> None:
        calls = {"count": 0}

        pkg = types.ModuleType("mlx_whisper")
        load_mod = types.ModuleType("mlx_whisper.load_models")

        def fake_load_model(_repo: str) -> None:
            calls["count"] += 1
            # 1回目は長くブロックして「待機側タイムアウト」を再現する。
            if calls["count"] == 1:
                time.sleep(0.25)

        load_mod.load_model = fake_load_model
        pkg.load_models = load_mod
        sys.modules["mlx_whisper"] = pkg
        sys.modules["mlx_whisper.load_models"] = load_mod

        worker_result: list[str] = []
        worker_error: list[Exception] = []

        def run_first_loader() -> None:
            try:
                worker_result.append(
                    streaming.get_or_load_model("large-v3", timeout_sec=1.0)
                )
            except Exception as e:  # pragma: no cover - failure path assertion below
                worker_error.append(e)

        t = threading.Thread(target=run_first_loader, daemon=True)
        t.start()
        time.sleep(0.03)

        repo = streaming.get_or_load_model("large-v3", timeout_sec=0.1)
        self.assertEqual("mlx-community/whisper-large-v3-mlx", repo)

        t.join(timeout=1.0)
        self.assertFalse(t.is_alive(), "first loader thread should finish")
        self.assertFalse(worker_error, f"unexpected worker error: {worker_error}")
        self.assertEqual(
            "mlx-community/whisper-large-v3-mlx",
            worker_result[0],
        )
        self.assertGreaterEqual(calls["count"], 2, "should retry load after stale timeout")
        self.assertEqual("mlx-community/whisper-large-v3-mlx", streaming._loaded_repo)
        self.assertIsNone(streaming._loading_repo)


if __name__ == "__main__":
    unittest.main()
