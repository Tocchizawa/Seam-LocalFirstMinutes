from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.transcribe import streaming


class WhisperModelManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache = tempfile.TemporaryDirectory()
        self.old_cache = os.environ.get("HF_HUB_CACHE")
        os.environ["HF_HUB_CACHE"] = self.cache.name
        self._reset_download_state()
        streaming._loaded_repo = None

    def tearDown(self) -> None:
        self._reset_download_state()
        if self.old_cache is None:
            os.environ.pop("HF_HUB_CACHE", None)
        else:
            os.environ["HF_HUB_CACHE"] = self.old_cache
        self.cache.cleanup()

    @staticmethod
    def _reset_download_state() -> None:
        with streaming._download_condition:
            streaming._download_active_repo = None
            streaming._download_status.update({
                "state": "idle",
                "model": None,
                "repo": None,
                "current_bytes": 0,
                "total_bytes": 0,
                "percent": None,
                "speed_bytes_per_sec": None,
                "error": None,
            })
            streaming._download_condition.notify_all()

    def _seed_cache(
        self, model_name: str, weights_name: str = "weights.npz"
    ) -> tuple[str, Path]:
        repo = streaming.MLX_REPO_MAP[model_name]
        model_dir = Path(self.cache.name) / f"models--{repo.replace('/', '--')}"
        revision = "test-revision"
        snapshot = model_dir / "snapshots" / revision
        snapshot.mkdir(parents=True)
        (model_dir / "refs").mkdir()
        (model_dir / "refs" / "main").write_text(revision, encoding="utf-8")
        (snapshot / weights_name).write_bytes(b"weights")
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
        return repo, snapshot

    def test_cached_model_is_catalogued_and_load_target_is_local(self) -> None:
        repo, snapshot = self._seed_cache("medium")
        with streaming._download_condition:
            streaming._download_status.update({
                "state": "error",
                "model": "tiny",
                "repo": streaming.MLX_REPO_MAP["tiny"],
                "error": "previous failure",
            })

        result = streaming.get_whisper_model_catalog()
        model = next(item for item in result["models"] if item["name"] == "medium")

        self.assertTrue(model["downloaded"])
        self.assertEqual(model["state"], "downloaded")
        self.assertEqual(streaming.ensure_model_downloaded("medium"), str(snapshot))
        self.assertEqual(model["repo"], repo)
        self.assertGreater(model["size_bytes"], 0)
        self.assertEqual(streaming.get_whisper_download_status()["state"], "ready")
        self.assertEqual(streaming.get_whisper_download_status()["model"], "medium")

    def test_safetensors_model_is_catalogued_and_load_target_is_local(self) -> None:
        repo, snapshot = self._seed_cache("small", "weights.safetensors")

        result = streaming.get_whisper_model_catalog()
        model = next(item for item in result["models"] if item["name"] == "small")

        self.assertTrue(model["downloaded"])
        self.assertEqual(model["state"], "downloaded")
        self.assertEqual(streaming.ensure_model_downloaded("small"), str(snapshot))
        self.assertEqual(model["repo"], repo)

    def test_valid_snapshot_is_reused_when_main_ref_is_missing(self) -> None:
        _repo, snapshot = self._seed_cache("base")
        (snapshot.parent.parent / "refs" / "main").unlink()

        with patch(
            "huggingface_hub.snapshot_download",
            side_effect=AssertionError("cached model must not be downloaded again"),
        ):
            self.assertEqual(streaming.ensure_model_downloaded("base"), str(snapshot))

    def test_download_progress_is_published_from_byte_bar(self) -> None:
        repo = streaming.MLX_REPO_MAP["tiny"]
        with streaming._download_condition:
            token = streaming._claim_download("tiny", repo)

        bar = streaming._DownloadTqdm(total=100, unit="B")
        bar.update(25)
        status = streaming.get_whisper_download_status()
        bar.close()

        self.assertEqual(status["model"], "tiny")
        self.assertEqual(status["current_bytes"], 25)
        self.assertEqual(status["total_bytes"], 100)
        self.assertEqual(status["percent"], 25.0)
        self.assertIn("speed_bytes_per_sec", status)
        self.assertEqual(token, streaming._download_token)

    def test_download_speed_is_published(self) -> None:
        with streaming._download_condition:
            streaming._claim_download("tiny", streaming.MLX_REPO_MAP["tiny"])

        streaming._update_download_progress(50, 100, 12_345.6)

        self.assertEqual(
            streaming.get_whisper_download_status()["speed_bytes_per_sec"],
            12_345.6,
        )

    def test_delete_removes_cached_model(self) -> None:
        _repo, snapshot = self._seed_cache("base")
        self.assertTrue(snapshot.exists())

        streaming.delete_whisper_model("base")

        self.assertFalse(snapshot.exists())
        model = next(
            item for item in streaming.get_whisper_model_catalog()["models"]
            if item["name"] == "base"
        )
        self.assertFalse(model["downloaded"])

    def test_background_download_updates_status_until_ready(self) -> None:
        repo = streaming.MLX_REPO_MAP["small"]

        def fake_snapshot_download(*, repo_id, cache_dir, tqdm_class):
            self.assertEqual(repo_id, repo)
            self.assertEqual(Path(cache_dir), Path(self.cache.name))
            self._seed_cache("small")
            bar = tqdm_class(total=100, unit="B")
            bar.update(40)
            bar.close()

        with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download):
            initial = streaming.start_whisper_model_download("small")
            self.assertIn(initial["state"], {"downloading", "ready"})
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                status = streaming.get_whisper_download_status()
                if status["state"] == "ready":
                    break
                time.sleep(0.01)

        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["model"], "small")
        self.assertEqual(status["percent"], 100.0)


if __name__ == "__main__":
    unittest.main()
