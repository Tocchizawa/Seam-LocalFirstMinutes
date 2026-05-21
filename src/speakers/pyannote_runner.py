from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import keyring
import numpy as np

from src.config import config

logger = logging.getLogger(__name__)

# pyannote.audio が hook へ渡してくる step 名のサブストリングマッチで、
# (進捗範囲, 表示ラベル) を割り当てる。pyannote 3.x は
# "speaker_segmentation" / "speaker_embedding" / "speaker_counting" /
# "discrete_diarization" 等を発火する。
_PYANNOTE_STAGE_RANGES: tuple[tuple[str, tuple[float, float], str], ...] = (
    ("segmentation", (0.0, 0.45), "音声を分割中"),
    ("embedd", (0.45, 0.90), "話者特徴を抽出中"),
    ("counting", (0.90, 0.92), "話者数を推定中"),
    ("clustering", (0.92, 0.98), "話者をクラスタリング中"),
    ("discrete_diarization", (0.92, 0.98), "話者をクラスタリング中"),
)


class _PyannoteProgressHook:
    """pyannote.audio Pipeline の進捗フック。

    pipeline は内部の各ステージで hook(step_name, artifact, total=, completed=)
    を呼ぶ。本フックはステージ名から大まかな進捗範囲を割り当て、コールバックに
    0.0..1.0 のフラットな進捗値と日本語メッセージを流す。
    """

    def __init__(self, on_progress):
        self._on_progress = on_progress
        self._last_prog = 0.0

    @staticmethod
    def _match(step_name: str) -> tuple[tuple[float, float] | None, str]:
        s = (step_name or "").lower()
        for key, rng, label in _PYANNOTE_STAGE_RANGES:
            if key in s:
                return rng, label
        return None, step_name

    def __call__(
        self,
        step_name,
        step_artifact=None,
        file=None,
        total=None,
        completed=None,
    ) -> None:
        if not self._on_progress:
            return
        rng, label = self._match(step_name)
        if rng is None:
            return
        start, end = rng
        if total and completed is not None and total > 0:
            frac = max(0.0, min(1.0, float(completed) / float(total)))
            prog = start + (end - start) * frac
            msg = f"{label}... {int(completed)}/{int(total)}"
        else:
            prog = start
            msg = f"{label}..."
        if prog < self._last_prog:
            prog = self._last_prog
        else:
            self._last_prog = prog
        try:
            self._on_progress(prog, msg)
        except Exception:
            pass

KEYRING_SERVICE = "seam-app"
HF_TOKEN_KEY = "hf_token"

_pipeline_lock = threading.Lock()
_pipeline = None
_pipeline_signature: tuple[str, str] | None = None


def get_hf_token() -> str | None:
    try:
        token = keyring.get_password(KEYRING_SERVICE, HF_TOKEN_KEY)
        if token:
            return token
    except Exception as e:
        logger.warning("keyring read failed: %s", e)
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")


def set_hf_token(token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, HF_TOKEN_KEY, token.strip())
    # トークン更新時はキャッシュをクリアして次回ロード
    reset_pipeline()


def delete_hf_token() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, HF_TOKEN_KEY)
    except Exception:
        pass
    reset_pipeline()


def has_hf_token() -> bool:
    return bool(get_hf_token())


def reset_pipeline() -> None:
    global _pipeline, _pipeline_signature
    with _pipeline_lock:
        _pipeline = None
        _pipeline_signature = None


def _resolve_device(setting: str = "auto"):
    import torch
    if setting == "cpu":
        return torch.device("cpu")
    if setting == "mps":
        if torch.backends.mps.is_available():
            return torch.device("mps")
        logger.warning("MPS not available, falling back to CPU")
        return torch.device("cpu")
    # auto: prefer MPS on Apple Silicon
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _get_pipeline():
    """Diarization pipeline を遅延ロード (キャッシュ付き)。"""
    global _pipeline, _pipeline_signature
    model_name = str(
        config.get("whisper", "speaker_memory", "pyannote_model",
                   default="pyannote/speaker-diarization-3.1")
    )
    device_setting = str(
        config.get("whisper", "speaker_memory", "pyannote_device", default="auto")
    )

    with _pipeline_lock:
        sig = (model_name, device_setting)
        if _pipeline is not None and _pipeline_signature == sig:
            return _pipeline

        token = get_hf_token()
        if not token:
            raise RuntimeError(
                "HuggingFace トークンが未設定です。設定画面でトークンを登録してください。"
            )

        from pyannote.audio import Pipeline
        from src.startup_progress import emit as _emit_progress

        _emit_progress(
            "models",
            "話者分離モデルをロード中 (初回はダウンロード)",
            0.85,
            detail=model_name,
        )

        try:
            pipeline = Pipeline.from_pretrained(model_name, token=token)
        except Exception as e:
            raise RuntimeError(
                f"pyannote モデル '{model_name}' のロードに失敗しました: {e}\n"
                "HF でモデル利用規約を承認しているか、トークンが正しいか確認してください。"
            ) from e

        device = _resolve_device(device_setting)
        try:
            pipeline.to(device)
        except Exception as e:
            logger.warning("Failed to move pipeline to %s: %s. Using CPU.", device, e)
            import torch
            device = torch.device("cpu")
            pipeline.to(device)

        _pipeline = pipeline
        _pipeline_signature = sig
        logger.info("pyannote pipeline loaded: %s on %s", model_name, device)
        _emit_progress(
            "models",
            "話者分離モデルのロード完了",
            0.92,
            detail=f"{model_name} on {device}",
        )
        return _pipeline


def diarize_with_embeddings(
    wav_path: str | Path,
    on_progress=None,
) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    """Run pyannote diarization. Returns (turns, centroids).

    turns: [{"start": float, "end": float, "speaker": "SPEAKER_00", ...}, ...]
    centroids: {"SPEAKER_00": ndarray (embedding_dim,), ...}  L2 正規化済み

    on_progress(prog: float 0..1, message: str | None) を渡すと、pyannote 内部の
    各ステージ進捗を細かく通知する。
    """
    pipeline = _get_pipeline()
    kwargs: dict[str, Any] = {}
    min_speakers = config.get("whisper", "speaker_memory", "pyannote_min_speakers", default=None)
    max_speakers = config.get("whisper", "speaker_memory", "pyannote_max_speakers", default=None)
    if min_speakers:
        kwargs["min_speakers"] = int(min_speakers)
    if max_speakers:
        kwargs["max_speakers"] = int(max_speakers)

    if on_progress is not None:
        kwargs["hook"] = _PyannoteProgressHook(on_progress)

    try:
        raw = pipeline(str(wav_path), **kwargs)
    except TypeError as e:
        # hook 引数を受け付けない pyannote 版へのフォールバック
        if "hook" in kwargs and "hook" in str(e):
            logger.warning(
                "pyannote.audio が hook 引数を受け付けません。進捗表示なしで継続します: %s", e,
            )
            kwargs.pop("hook", None)
            raw = pipeline(str(wav_path), **kwargs)
        else:
            raise

    # pyannote.audio 4.x: DiarizeOutput(speaker_diarization, speaker_embeddings, ...)
    # 3.x: tuple (diarization, embeddings) when return_embeddings=True, else Annotation
    diarization = None
    embeddings = None
    if hasattr(raw, "speaker_diarization"):
        diarization = raw.speaker_diarization
        embeddings = getattr(raw, "speaker_embeddings", None)
    elif isinstance(raw, tuple) and len(raw) == 2:
        diarization, embeddings = raw
    else:
        diarization = raw

    turns: list[dict[str, Any]] = []
    for turn, _track, speaker in diarization.itertracks(yield_label=True):
        turns.append({
            "start": float(turn.start),
            "end": float(turn.end),
            "speaker": str(speaker),
        })

    centroids: dict[str, np.ndarray] = {}
    if embeddings is not None:
        labels = list(diarization.labels())
        emb_arr = np.asarray(embeddings)
        for i, label in enumerate(labels):
            if i >= emb_arr.shape[0]:
                break
            vec = np.asarray(emb_arr[i]).reshape(-1).astype(np.float32)
            if not np.all(np.isfinite(vec)):
                continue
            norm = float(np.linalg.norm(vec))
            if norm <= 1e-8:
                continue
            centroids[str(label)] = vec / norm

    return turns, centroids


def is_available() -> bool:
    """pyannote.audio パッケージが import 可能か。"""
    try:
        import pyannote.audio  # noqa: F401
        return True
    except Exception:
        return False
