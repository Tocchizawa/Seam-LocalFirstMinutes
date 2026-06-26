"""VAD ベースのストリーミング文字起こし (Apple MLX / mlx-whisper)。

録音中に音声をチャンク化して逐次 Whisper にかける。
- VAD (silero or energy) で発話区切りを検出
- 無音が一定時間続いたら chunk 確定 → Whisper
- max_chunk_ms に達した場合は smart-cut (直近の最も静かな点で切る)
- Whisper 自身が segment を文単位に分割するので、出力はシームレス
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np

from src.speakers import speaker_memory
from src.transcribe.hallucination_filter import HallucinationFilter

logger = logging.getLogger(__name__)


SAMPLE_RATE = 16000


# ─── システム負荷を抑えるための初期設定 ──────────────────────────
# MLX (Apple Metal) のメモリキャップを relaxed モードで設定。
# 必要に応じてキャップを超えられるが、通常時は他アプリ用の余白を確保。
try:
    import mlx.core as _mx  # type: ignore
    import psutil as _psutil  # type: ignore
    _total_gb = _psutil.virtual_memory().total / (1024 ** 3)
    # システム RAM の 40% を上限に。relaxed=True なので必要時は超えられる
    # 16GB Mac → 約 6GB / 32GB → 約 13GB
    _cap_gb = min(20, max(4, int(_total_gb * 0.4)))
    _cap_bytes = _cap_gb * 1024 * 1024 * 1024
    try:
        if hasattr(_mx, "metal") and hasattr(_mx.metal, "set_memory_limit"):
            _mx.metal.set_memory_limit(_cap_bytes, relaxed=True)
            logger.info("MLX Metal memory cap: %d GB (relaxed)", _cap_gb)
        # キャッシュも緩めに
        if hasattr(_mx, "metal") and hasattr(_mx.metal, "set_cache_limit"):
            _mx.metal.set_cache_limit(int(_cap_bytes * 0.5))
    except Exception as _e:
        logger.debug("MLX memory cap not applied: %s", _e)
except Exception:
    pass


# ─── mlx-whisper モデル管理 ───────────────────────────────────────
# Apple MLX (Metal) で Whisper を動かす。CPU 比 5〜8x。
# モデル名 → HuggingFace MLX repo のマッピング。

MLX_REPO_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large": "mlx-community/whisper-large-v3-mlx",
    "large-v1": "mlx-community/whisper-large-v1-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def _resolve_repo(model_name: str) -> str:
    """ローカル名 → HF repo path。フルパス指定もそのまま通す。"""
    if "/" in model_name:
        return model_name
    return MLX_REPO_MAP.get(model_name, MLX_REPO_MAP["large-v3"])


def _hf_cache_root() -> Path:
    root = (
        os.environ.get("HF_HUB_CACHE")
        or os.environ.get("HUGGINGFACE_HUB_CACHE")
        or (
            str(Path(os.environ["HF_HOME"]) / "hub")
            if os.environ.get("HF_HOME")
            else None
        )
        or str(Path.home() / ".cache" / "huggingface" / "hub")
    )
    return Path(root)


def _repo_cache_dir(repo: str) -> Path:
    return _hf_cache_root() / f"models--{repo.replace('/', '--')}"


def _estimate_repo_cache_bytes(repo: str) -> int:
    """repo キャッシュ配下の実体ファイルサイズ合計を返す。

    snapshot 内の symlink は blobs と二重計上になるため除外する。
    """
    if "/" not in repo:
        return 0
    model_dir = _repo_cache_dir(repo)
    if not model_dir.exists():
        return 0

    total = 0
    stack = [model_dir]
    while stack:
        cur = stack.pop()
        try:
            for ent in cur.iterdir():
                try:
                    if ent.is_symlink():
                        continue
                    if ent.is_dir():
                        stack.append(ent)
                    elif ent.is_file():
                        total += ent.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
    return total


def _resolve_cached_snapshot(repo: str) -> str | None:
    """HF Hub キャッシュ内に既存 snapshot があればローカルパスを返す。"""
    if "/" not in repo:
        return None

    model_dir = _repo_cache_dir(repo)
    ref_main = model_dir / "refs" / "main"
    if not ref_main.exists():
        return None
    try:
        revision = ref_main.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not revision:
        return None

    snap = model_dir / "snapshots" / revision
    if not snap.exists():
        return None
    if not (snap / "weights.npz").exists():
        return None
    if not (snap / "config.json").exists():
        return None
    return str(snap)


_loaded_repo: str | None = None
_load_lock = threading.Lock()
_load_event = threading.Event()
_loading_repo: str | None = None
_loading_started_at: float | None = None
_loading_token: int = 0
_load_error: str | None = None
_loading_progress_bytes: int = 0
_loading_progress_at: float | None = None


def get_or_load_model(
    model_name: str,
    timeout_sec: float = 120.0,
    *,
    allow_stale_takeover: bool = False,
) -> str:
    """指定モデルを mlx-whisper にロードさせる(repo path を返す)。

    mlx-whisper は load_model() に LRU キャッシュを持つので、同じ repo で
    再度呼んでも実ロードは初回のみ。

    allow_stale_takeover=True は復旧用の明示モード。通常 caller は timeout_sec を
    待機上限として扱い、他 thread の loader が固着していても自動引き継ぎしない。
    """
    global _loaded_repo, _loading_repo, _loading_started_at, _loading_token, _load_error
    global _loading_progress_bytes, _loading_progress_at
    repo = _resolve_repo(model_name)
    timeout_sec = max(0.1, float(timeout_sec))
    deadline = time.monotonic() + timeout_sec

    while True:
        should_load = False
        load_token = 0
        with _load_lock:
            if _loaded_repo == repo and _load_event.is_set():
                return repo
            if _loading_repo is None:
                _loading_repo = repo
                _loading_started_at = time.monotonic()
                _loading_token += 1
                load_token = _loading_token
                _load_error = None
                _loading_progress_bytes = _estimate_repo_cache_bytes(repo)
                _loading_progress_at = _loading_started_at
                _load_event.clear()
                should_load = True

        if should_load:
            try:
                from mlx_whisper.load_models import load_model
                load_target = _resolve_cached_snapshot(repo) or repo
                t0 = time.time()
                logger.info(
                    "Loading mlx-whisper model: %s (target=%s, token=%d)",
                    repo,
                    load_target,
                    load_token,
                )
                load_model(load_target)
                committed = False
                with _load_lock:
                    # stale load は結果を書き戻さない (タイムアウト救済後の競合を防ぐ)
                    if _loading_repo == repo and _loading_token == load_token:
                        _loaded_repo = repo
                        _loading_repo = None
                        _loading_started_at = None
                        _load_error = None
                        _loading_progress_bytes = 0
                        _loading_progress_at = None
                        _load_event.set()
                        committed = True
                if committed:
                    logger.info("mlx-whisper loaded in %.1fs (repo=%s, token=%d)", time.time() - t0, repo, load_token)
                    return repo
                logger.warning(
                    "mlx-whisper load finished but lease moved; ignoring stale result "
                    "(repo=%s, token=%d)",
                    repo,
                    load_token,
                )
                continue
            except Exception as e:
                committed = False
                with _load_lock:
                    if _loading_repo == repo and _loading_token == load_token:
                        _loading_repo = None
                        _loading_started_at = None
                        _load_error = str(e)
                        _loading_progress_bytes = 0
                        _loading_progress_at = None
                        _load_event.set()
                        committed = True
                if committed:
                    logger.error("mlx-whisper load failed: %s", e)
                else:
                    logger.warning(
                        "mlx-whisper load failed in stale attempt; ignoring stale failure "
                        "(repo=%s, token=%d): %s",
                        repo,
                        load_token,
                        e,
                    )
                raise

        remain = deadline - time.monotonic()
        if remain <= 0:
            took_over_stale = False
            with _load_lock:
                loading_repo = _loading_repo
                loading_started_at = _loading_started_at
                loading_token = _loading_token
                load_error = _load_error
                loading_progress_bytes = _loading_progress_bytes
                loading_progress_at = _loading_progress_at
                now_mono = time.monotonic()
                load_age = (now_mono - loading_started_at) if loading_started_at else 0.0

            # 復旧モードでは、ダウンロード/キャッシュ書き込みが進行している間だけ
            # lease を延長する。通常 caller は timeout_sec を超えて待たない。
            if loading_repo and loading_started_at:
                current_cache_bytes = _estimate_repo_cache_bytes(loading_repo)
                cache_delta = current_cache_bytes - loading_progress_bytes
                if cache_delta > 0:
                    with _load_lock:
                        if _loading_repo == loading_repo and _loading_token == loading_token:
                            _loading_progress_bytes = current_cache_bytes
                            _loading_progress_at = now_mono
                    logger.info(
                        "mlx-whisper download progressing; extending wait "
                        "(loading=%s, requested=%s, token=%d, +%.1fMB, total=%.1fMB)",
                        loading_repo,
                        repo,
                        loading_token,
                        cache_delta / (1024 * 1024),
                        current_cache_bytes / (1024 * 1024),
                    )
                    if allow_stale_takeover:
                        deadline = time.monotonic() + timeout_sec
                        continue

            idle_since_progress = (
                (now_mono - loading_progress_at)
                if loading_progress_at is not None
                else load_age
            )

            with _load_lock:
                # ロード担当が固着した場合は貸与を失効させ、次ループで再ロードを試みる。
                if (
                    allow_stale_takeover
                    and loading_repo
                    and loading_started_at
                    and load_age >= timeout_sec
                    and idle_since_progress >= timeout_sec
                ):
                    _loading_repo = None
                    _loading_started_at = None
                    _load_error = (
                        f"stale loader expired after {load_age:.1f}s "
                        f"(repo={loading_repo}, token={loading_token}, "
                        f"idle={idle_since_progress:.1f}s)"
                    )
                    _loading_progress_bytes = 0
                    _loading_progress_at = None
                    _load_event.set()
                    took_over_stale = True
                    load_error = _load_error

            if took_over_stale:
                logger.warning(
                    "mlx-whisper loader seems stuck; retrying fresh load "
                    "(repo=%s, stale_token=%d, age=%.1fs)",
                    repo,
                    loading_token,
                    load_age,
                )
                deadline = time.monotonic() + timeout_sec
                continue

            msg = (
                f"Timed out waiting for mlx-whisper model load after {timeout_sec:.0f}s "
                f"(requested={repo}, loading={loading_repo or 'none'})"
            )
            if load_error:
                msg = f"{msg}: last_error={load_error}"
            raise TimeoutError(msg)

        _load_event.wait(timeout=min(1.0, remain))


def preload_model(model_name: str) -> None:
    """非同期に呼ぶ用: バックグラウンドでモデルをプリロード。"""
    def _do_preload():
        try:
            get_or_load_model(model_name)
        except Exception as e:
            logger.error("Preload failed (%s): %s", model_name, e)
    t = threading.Thread(target=_do_preload, daemon=True, name="whisper-preload")
    t.start()


# Whisper の initial_prompt は ~244 トークン上限。日本語は1文字 ≈ 1〜2 トークンなので
# 概ね 220 文字を上限と見ておけば安全。
PROMPT_MAX_CHARS = 220
PROMPT_RECENT_CHARS = 80  # 直近の文脈に確保する文字数


def build_initial_prompt(glossary: list[str], recent_text: str = "") -> str:
    """用語集と直近のテキストから whisper の initial_prompt を組み立てる。

    用語集を頭に置いて文脈に直近テキストを後ろにつなぐ。文字数上限内に収める。
    """
    terms = [g.strip() for g in glossary if g and g.strip()]
    glossary_str = "、".join(terms)
    recent = (recent_text or "").strip()
    # 直近を後ろに連結。用語集が長すぎる場合は用語集を優先して切り詰め。
    available = max(0, PROMPT_MAX_CHARS - PROMPT_RECENT_CHARS)
    if len(glossary_str) > available:
        glossary_str = glossary_str[:available]
    if recent:
        budget = PROMPT_MAX_CHARS - len(glossary_str) - 1  # separator
        if budget > 0:
            recent = recent[-budget:]
            return f"{glossary_str} {recent}".strip() if glossary_str else recent
    return glossary_str


def estimate_chunker_pending_sec(chunker: object, sample_rate: int = SAMPLE_RATE) -> float:
    """chunker 内に未確定で保持されている末尾 audio 秒数を返す。"""
    pending = 0.0
    frames_ms = getattr(chunker, "_frames_ms", None)
    if frames_ms is not None:
        try:
            pending += max(0.0, float(frames_ms) / 1000.0)
        except Exception:
            pass
    stream_buf = getattr(chunker, "_stream_buffer", None)
    if stream_buf is not None:
        try:
            pending += max(0.0, len(stream_buf) / sample_rate)
        except Exception:
            pass
    pending_emit = getattr(chunker, "_pending_emit", None)
    if pending_emit is not None:
        try:
            pending += sum(
                max(0.0, len(ch) / sample_rate) for ch in pending_emit if ch is not None
            )
        except Exception:
            pass
    return pending


def compute_chunk_start_offset(
    total_audio_sec: float,
    chunk_samples: int,
    chunker: object,
    sample_rate: int = SAMPLE_RATE,
) -> float:
    """feed 済み総時間と chunker の保留分から、確定 chunk の開始時刻を計算する。"""
    chunk_dur = chunk_samples / sample_rate
    pending_after_emit = estimate_chunker_pending_sec(chunker, sample_rate=sample_rate)
    chunk_end = max(0.0, total_audio_sec - pending_after_emit)
    return max(0.0, chunk_end - chunk_dur)


class VADChunker:
    """エネルギーベースの VAD でチャンクを切り出す。"""

    def __init__(self,
                 sample_rate: int = SAMPLE_RATE,
                 rms_threshold: float = 0.005,
                 silence_duration_ms: int = 500,
                 min_chunk_ms: int = 1000,
                 max_chunk_ms: int = 12000,
                 # max に達した時、直近この時間内で最も静かな点を切れ目にする
                 smart_cut_lookback_ms: int = 1500) -> None:
        self.sample_rate = sample_rate
        self.rms_threshold = rms_threshold
        self.silence_duration_ms = silence_duration_ms
        self.min_chunk_ms = min_chunk_ms
        self.max_chunk_ms = max_chunk_ms
        self.smart_cut_lookback_ms = smart_cut_lookback_ms
        # 状態
        self._frames: list[np.ndarray] = []
        self._frame_rms: list[float] = []  # 各 frame の RMS (smart cut 用)
        self._frames_ms = 0.0
        self._silence_after_speech_ms = 0.0
        self._had_speech = False
        # 診断用
        self.last_rms: float = 0.0
        self.peak_rms: float = 0.0

    def feed(self, frame: np.ndarray) -> np.ndarray | None:
        """float32 [-1,1] mono フレームを投入。chunk 確定なら返す。"""
        if frame is None or len(frame) == 0:
            return None
        frame_ms = (len(frame) / self.sample_rate) * 1000.0
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        self.last_rms = rms
        if rms > self.peak_rms:
            self.peak_rms = rms
        is_speech = rms >= self.rms_threshold

        self._frames.append(frame)
        self._frame_rms.append(rms)
        self._frames_ms += frame_ms

        if is_speech:
            self._had_speech = True
            self._silence_after_speech_ms = 0.0
        elif self._had_speech:
            self._silence_after_speech_ms += frame_ms

        if self._had_speech:
            if self._silence_after_speech_ms >= self.silence_duration_ms:
                return self._consume()
            if self._frames_ms >= self.max_chunk_ms:
                # max 到達: 直近 lookback_ms で最も静かな所で切る
                return self._consume_smart()
        else:
            # 発話なしのまま max に達したら捨てる
            if self._frames_ms >= self.max_chunk_ms:
                self._reset()
        return None

    def flush(self) -> np.ndarray | None:
        """残バッファを強制 chunk 化(発話あり時のみ)。"""
        if not self._frames:
            return None
        if self._had_speech:
            return self._consume()
        self._reset()
        return None

    def _consume(self) -> np.ndarray | None:
        if not self._frames:
            return None
        chunk = np.concatenate(self._frames)
        chunk_ms = self._frames_ms
        self._reset()
        if chunk_ms >= self.min_chunk_ms:
            return chunk
        return None

    def _consume_smart(self) -> np.ndarray | None:
        """max_chunk_ms 到達時、直近 lookback_ms 内で最も静かなフレームで切る。

        一文の途中で切れて変な所で切れ目が入るのを抑える。
        """
        n = len(self._frames)
        if n == 0:
            return None
        # 各フレームの ms (= frame_ms 一定とは限らないが、概ね均一なので近似)
        avg_frame_ms = self._frames_ms / n if n > 0 else 100.0
        lookback_frames = max(1, int(self.smart_cut_lookback_ms / avg_frame_ms))
        # min_chunk_ms より短くならないようにする (cut 位置の最小 index)
        min_keep_frames = max(1, int(self.min_chunk_ms / avg_frame_ms))
        start = max(min_keep_frames, n - lookback_frames)
        if start >= n - 1:
            # lookback できる範囲がなければ通常 consume
            return self._consume()
        # start..n-1 の中で RMS 最小のフレームを cut 点とする
        sub_rms = self._frame_rms[start:n]
        rel_idx = int(np.argmin(sub_rms))
        cut_idx = start + rel_idx + 1  # この frame まで含めて切る
        if cut_idx <= 0 or cut_idx >= n:
            return self._consume()
        # cut_idx 以降は次回チャンクへ
        head_frames = self._frames[:cut_idx]
        tail_frames = self._frames[cut_idx:]
        tail_rms = self._frame_rms[cut_idx:]
        head_ms = sum(len(f) for f in head_frames) / self.sample_rate * 1000.0
        tail_ms = sum(len(f) for f in tail_frames) / self.sample_rate * 1000.0
        chunk = np.concatenate(head_frames) if head_frames else None
        # 状態を tail で初期化
        self._frames = list(tail_frames)
        self._frame_rms = list(tail_rms)
        self._frames_ms = tail_ms
        self._silence_after_speech_ms = 0.0
        # tail 側に発話があったかは、 tail の RMS が threshold を超えるフレームが
        # 1つでもあれば had_speech とみなす
        self._had_speech = any(r >= self.rms_threshold for r in tail_rms)
        if chunk is None or head_ms < self.min_chunk_ms:
            return None
        return chunk

    def _reset(self) -> None:
        self._frames = []
        self._frame_rms = []
        self._frames_ms = 0.0
        self._silence_after_speech_ms = 0.0
        self._had_speech = False


class SileroVADChunker:
    """Silero VAD ベースのチャンカー。エネルギーベースよりノイズ耐性が高い。

    32ms (512 sample) 窓で speech_prob を取り、threshold 超過で speech と判定。
    silence_duration_ms 連続で speech 未満なら chunk 確定 → 返す。
    max_chunk_ms に達した場合は smart-cut で直近の speech_prob 最小点を切れ目にする。
    """

    WINDOW_SAMPLES = 512  # silero-vad の 16kHz 推奨ウィンドウ
    DEFAULT_THRESHOLD = 0.5

    def __init__(self,
                 sample_rate: int = SAMPLE_RATE,
                 threshold: float = DEFAULT_THRESHOLD,
                 silence_duration_ms: int = 500,
                 min_chunk_ms: int = 1000,
                 max_chunk_ms: int = 12000,
                 smart_cut_lookback_ms: int = 1500) -> None:
        if sample_rate != 16000:
            raise ValueError("SileroVADChunker requires 16kHz input")
        self.sample_rate = sample_rate
        self.threshold = float(threshold)
        self.silence_duration_ms = silence_duration_ms
        self.min_chunk_ms = min_chunk_ms
        self.max_chunk_ms = max_chunk_ms
        self.smart_cut_lookback_ms = smart_cut_lookback_ms

        self._model = None
        self._model_lock = threading.Lock()

        # 入力 buffer (window_samples 揃うまで貯める)
        self._stream_buffer: np.ndarray = np.zeros(0, dtype=np.float32)

        # 確定した window 単位の状態
        self._frames: list[np.ndarray] = []
        self._frame_speech_prob: list[float] = []
        self._frames_ms: float = 0.0
        self._silence_after_speech_ms: float = 0.0
        self._had_speech: bool = False

        # 1 feed で複数 chunk が確定した場合の繰り越し
        self._pending_emit: list[np.ndarray] = []

        # 診断用
        self.last_rms: float = 0.0
        self.peak_rms: float = 0.0
        self.last_speech_prob: float = 0.0

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is not None:
                return
            from silero_vad import load_silero_vad
            self._model = load_silero_vad()
            logger.info("Silero VAD loaded")

    def feed(self, frame: np.ndarray) -> np.ndarray | None:
        if frame is None or len(frame) == 0:
            return self._pop_pending()
        try:
            self._ensure_model()
        except Exception as e:
            logger.error("Silero VAD load failed: %s", e)
            return None

        # buffer に積む → 512 サンプルずつ取り出して処理
        self._stream_buffer = np.concatenate([self._stream_buffer, frame.astype(np.float32, copy=False)])
        while len(self._stream_buffer) >= self.WINDOW_SAMPLES:
            window = self._stream_buffer[:self.WINDOW_SAMPLES].copy()
            self._stream_buffer = self._stream_buffer[self.WINDOW_SAMPLES:]
            chunk = self._process_window(window)
            if chunk is not None:
                self._pending_emit.append(chunk)
        return self._pop_pending()

    def _pop_pending(self) -> np.ndarray | None:
        if not self._pending_emit:
            return None
        return self._pending_emit.pop(0)

    def _process_window(self, window: np.ndarray) -> np.ndarray | None:
        import torch
        window_ms = (len(window) / self.sample_rate) * 1000.0
        rms = float(np.sqrt(np.mean(window ** 2)))
        self.last_rms = rms
        if rms > self.peak_rms:
            self.peak_rms = rms

        try:
            with torch.no_grad():
                tensor = torch.from_numpy(window)
                speech_prob = float(self._model(tensor, self.sample_rate).item())
        except Exception as e:
            logger.warning("Silero VAD inference error: %s", e)
            speech_prob = 0.0
        self.last_speech_prob = speech_prob

        is_speech = speech_prob >= self.threshold

        self._frames.append(window)
        self._frame_speech_prob.append(speech_prob)
        self._frames_ms += window_ms

        if is_speech:
            self._had_speech = True
            self._silence_after_speech_ms = 0.0
        elif self._had_speech:
            self._silence_after_speech_ms += window_ms

        if self._had_speech:
            if self._silence_after_speech_ms >= self.silence_duration_ms:
                return self._consume()
            if self._frames_ms >= self.max_chunk_ms:
                return self._consume_smart()
        else:
            if self._frames_ms >= self.max_chunk_ms:
                self._reset()
        return None

    def flush(self) -> np.ndarray | None:
        # 残 stream_buffer は最後の window として処理
        if len(self._stream_buffer) > 0:
            tail = self._stream_buffer
            self._stream_buffer = np.zeros(0, dtype=np.float32)
            # padding して window サイズに揃える
            if len(tail) < self.WINDOW_SAMPLES:
                tail = np.concatenate([tail, np.zeros(self.WINDOW_SAMPLES - len(tail), dtype=np.float32)])
            chunk = self._process_window(tail)
            if chunk is not None:
                self._pending_emit.append(chunk)
        # 未提出の pending を1つずつ返したいが、interface は単一なので最大1つ返す
        if self._pending_emit:
            return self._pending_emit.pop(0)
        # bufer 残があれば強制 chunk 化
        if self._frames and self._had_speech:
            return self._consume()
        self._reset()
        return None

    def _consume(self) -> np.ndarray | None:
        if not self._frames:
            return None
        chunk = np.concatenate(self._frames)
        chunk_ms = self._frames_ms
        self._reset()
        if chunk_ms >= self.min_chunk_ms:
            return chunk
        return None

    def _consume_smart(self) -> np.ndarray | None:
        n = len(self._frames)
        if n == 0:
            return None
        avg_frame_ms = self._frames_ms / n if n > 0 else 32.0
        lookback_frames = max(1, int(self.smart_cut_lookback_ms / avg_frame_ms))
        min_keep_frames = max(1, int(self.min_chunk_ms / avg_frame_ms))
        start = max(min_keep_frames, n - lookback_frames)
        if start >= n - 1:
            return self._consume()
        sub = self._frame_speech_prob[start:n]
        rel_idx = int(np.argmin(sub))
        cut_idx = start + rel_idx + 1
        if cut_idx <= 0 or cut_idx >= n:
            return self._consume()
        head_frames = self._frames[:cut_idx]
        tail_frames = self._frames[cut_idx:]
        tail_probs = self._frame_speech_prob[cut_idx:]
        head_ms = sum(len(f) for f in head_frames) / self.sample_rate * 1000.0
        tail_ms = sum(len(f) for f in tail_frames) / self.sample_rate * 1000.0
        chunk = np.concatenate(head_frames) if head_frames else None
        self._frames = list(tail_frames)
        self._frame_speech_prob = list(tail_probs)
        self._frames_ms = tail_ms
        self._silence_after_speech_ms = 0.0
        self._had_speech = any(p >= self.threshold for p in tail_probs)
        if chunk is None or head_ms < self.min_chunk_ms:
            return None
        return chunk

    def _reset(self) -> None:
        self._frames = []
        self._frame_speech_prob = []
        self._frames_ms = 0.0
        self._silence_after_speech_ms = 0.0
        self._had_speech = False


class StreamingTranscriber:
    """録音中に並行して動くストリーミング文字起こし。

    feed(samples_f32_16k) で 16kHz mono float32 サンプルを投入。
    VAD で chunk 確定したらワーカースレッドで mlx-whisper に投入。
    各 segment は on_segment コルーチンで通知。
    """

    def __init__(self,
                 model_name: str = "large-v3",
                 language: str = "ja",
                 on_segment: Callable[[dict], Awaitable[None]] | None = None,
                 session_id: str | None = None,
                 chunker_kwargs: dict | None = None,
                 max_queue_chunks: int = 0,
                 max_pending_audio_sec: float = 240.0,
                 model_load_timeout_sec: float = 120.0,
                 flush_join_timeout_sec: float = 120.0,
                 hallucination_cfg: dict | None = None,
                 glossary: list[str] | None = None,
                 corrections: list[tuple[str, str]] | None = None,
                 vad_provider: str = "silero") -> None:
        self.model_name = model_name
        self.language = language
        self._on_segment = on_segment
        self._session_id = session_id
        self._vad_provider = (vad_provider or "silero").lower()
        self._chunker = self._build_chunker(chunker_kwargs or {})
        self._glossary: list[str] = [g.strip() for g in (glossary or []) if g and g.strip()]
        # 長いキーから順に置換 (短いキーが部分一致して長いキーを破壊しないように)
        self._corrections: list[tuple[str, str]] = sorted(
            [(w.strip(), c.strip()) for w, c in (corrections or [])
             if w and c and w.strip() and c.strip() and w.strip() != c.strip()],
            key=lambda x: len(x[0]),
            reverse=True,
        )
        # 直近の確定テキスト末尾を保持し、次チャンクの prompt 文脈として使う
        self._recent_text_tail: str = ""
        self._recent_text_lock = threading.Lock()
        self._hallucination_filter = HallucinationFilter.from_config(hallucination_cfg)
        queue_maxsize = max(0, int(max_queue_chunks))
        self._queue: queue.Queue[tuple[np.ndarray, float] | None] = queue.Queue(maxsize=queue_maxsize)
        self._queue_maxsize = queue_maxsize
        self._max_pending_audio_sec = max_pending_audio_sec
        self._model_load_timeout_sec = max(5.0, float(model_load_timeout_sec))
        self._flush_join_timeout_sec = max(5.0, float(flush_join_timeout_sec))
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._active_generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._segments: list[dict] = []
        self._segments_lock = threading.Lock()
        self._total_audio_sec = 0.0
        self._repo: str | None = None
        self._model_error: str | None = None
        self._model_loaded = threading.Event()
        # 進捗トラッキング(UI に出す用)
        self._current_chunk_audio_sec: float = 0.0
        self._current_chunk_started_at: float | None = None
        self._recent_ratios: list[float] = []  # rolling: audio_sec / process_sec
        self._pending_audio_sec: float = 0.0
        self._pending_lock = threading.Lock()
        # 監視用: 直近に chunk を処理し終えた時刻(monotonic)。watchdog がハング検出に使う
        self._last_processed_at: float = 0.0
        self._last_feed_at: float = 0.0
        self._worker_restarts: int = 0
        self._last_restart_reason: str | None = None
        self._last_restart_at: float = 0.0
        self._dropped_chunks: int = 0
        self._dropped_audio_sec: float = 0.0
        self._transcribe_errors: int = 0
        self._filtered_segments: int = 0

    @property
    def segments(self) -> list[dict]:
        with self._segments_lock:
            return list(self._segments)

    @property
    def model_error(self) -> str | None:
        return self._model_error

    @property
    def is_model_loaded(self) -> bool:
        return self._model_loaded.is_set() and self._repo is not None

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def total_segments(self) -> int:
        with self._segments_lock:
            return len(self._segments)

    @property
    def last_rms(self) -> float:
        return self._chunker.last_rms

    @property
    def peak_rms(self) -> float:
        return self._chunker.peak_rms

    @property
    def rms_threshold(self) -> float:
        # SileroVADChunker は rms_threshold を持たないので 0.0 を返す
        return getattr(self._chunker, "rms_threshold", 0.0)

    @property
    def current_chunk_audio_sec(self) -> float:
        return self._current_chunk_audio_sec

    @property
    def current_processing_sec(self) -> float:
        if self._current_chunk_started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._current_chunk_started_at)

    @property
    def avg_speed_ratio(self) -> float:
        """直近の audio_sec / process_sec の平均(>1 で実時間より速い)"""
        if not self._recent_ratios:
            return 0.0
        return sum(self._recent_ratios) / len(self._recent_ratios)

    @property
    def pending_audio_sec(self) -> float:
        with self._pending_lock:
            return self._pending_audio_sec

    @property
    def worker_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    @property
    def worker_restarts(self) -> int:
        return self._worker_restarts

    @property
    def dropped_chunks(self) -> int:
        return self._dropped_chunks

    @property
    def dropped_audio_sec(self) -> float:
        return self._dropped_audio_sec

    def _build_chunker(self, kwargs: dict):
        provider = self._vad_provider
        # silero に必要なパラメータだけ抽出 (VADChunker と互換でない rms_threshold は除外)
        if provider == "silero":
            try:
                allowed = {
                    "sample_rate", "threshold", "silence_duration_ms",
                    "min_chunk_ms", "max_chunk_ms", "smart_cut_lookback_ms",
                }
                clean = {k: v for k, v in kwargs.items() if k in allowed}
                return SileroVADChunker(**clean)
            except Exception as e:
                logger.warning("Silero VAD init failed (%s), falling back to energy", e)
                self._vad_provider = "energy"
        # エネルギーベース
        allowed = {
            "sample_rate", "rms_threshold", "silence_duration_ms",
            "min_chunk_ms", "max_chunk_ms", "smart_cut_lookback_ms",
        }
        clean = {k: v for k, v in kwargs.items() if k in allowed}
        return VADChunker(**clean)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._running:
            return
        self._loop = loop
        self._running = True
        with self._segments_lock:
            self._segments = []
        self._total_audio_sec = 0.0
        # 既存の chunker パラメータを保持して再構築
        prev = self._chunker
        kwargs: dict = {
            "sample_rate": prev.sample_rate,
            "silence_duration_ms": prev.silence_duration_ms,
            "min_chunk_ms": prev.min_chunk_ms,
            "max_chunk_ms": prev.max_chunk_ms,
        }
        if hasattr(prev, "rms_threshold"):
            kwargs["rms_threshold"] = prev.rms_threshold
        if hasattr(prev, "threshold"):
            kwargs["threshold"] = prev.threshold
        if hasattr(prev, "smart_cut_lookback_ms"):
            kwargs["smart_cut_lookback_ms"] = prev.smart_cut_lookback_ms
        self._chunker = self._build_chunker(kwargs)
        self._model_loaded.clear()
        self._model_error = None
        self._recent_ratios = []
        self._pending_audio_sec = 0.0
        self._last_processed_at = 0.0
        self._last_feed_at = 0.0
        self._worker_restarts = 0
        self._last_restart_reason = None
        self._last_restart_at = 0.0
        self._dropped_chunks = 0
        self._dropped_audio_sec = 0.0
        self._transcribe_errors = 0
        self._filtered_segments = 0
        self._clear_queue()
        self._spawn_worker(reason="start", force=True)

    def feed(self, samples_f32: np.ndarray) -> None:
        """16kHz mono float32 サンプルを投入(任意のスレッドから)。"""
        if not self._running:
            return
        if samples_f32 is None or len(samples_f32) == 0:
            return
        self._last_feed_at = time.monotonic()
        self._total_audio_sec += len(samples_f32) / SAMPLE_RATE
        chunk = self._chunker.feed(samples_f32)
        while chunk is not None:
            self._enqueue(chunk)
            # SileroVADChunker は 1 feed で複数 chunk が確定し得るため、
            # 空 feed で pending emit を取り切る。
            chunk = self._chunker.feed(np.zeros(0, dtype=np.float32))

    def flush(self) -> list[dict]:
        """残バッファを処理してワーカー終了を待つ。全 segment を返す。"""
        if not self._running:
            return self.segments
        while True:
            chunk = self._chunker.flush()
            if chunk is None:
                break
            self._enqueue(chunk)
        # ワーカー終了シグナル
        self._running = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            self._drop_oldest_chunk("flush sentinel")
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                logger.warning("StreamingTranscriber could not enqueue flush sentinel")
        if self._worker:
            wait_sec = self._flush_join_timeout_sec
            deadline = time.monotonic() + wait_sec
            last_progress = self._last_processed_at
            while self._worker.is_alive():
                remain = deadline - time.monotonic()
                if remain <= 0:
                    current_progress = self._last_processed_at
                    if current_progress > last_progress:
                        progressed = current_progress - last_progress
                        logger.info(
                            "StreamingTranscriber flush still progressing "
                            "(+%.1fs). extending wait by %.0fs",
                            progressed,
                            wait_sec,
                        )
                        last_progress = current_progress
                        deadline = time.monotonic() + wait_sec
                        continue
                    self._model_error = (
                        f"文字起こしワーカーの終了待ちがタイムアウトしました"
                        f"({wait_sec:.0f}s, no progress)"
                    )
                    logger.warning("StreamingTranscriber worker join timed out (no progress)")
                    break
                self._worker.join(timeout=min(2.0, remain))
                current_progress = self._last_processed_at
                if current_progress > last_progress:
                    last_progress = current_progress
                    deadline = time.monotonic() + wait_sec
        return self.segments

    def cleanup(self) -> None:
        self._running = False
        # mlx-whisper のキャッシュは load_model 側で保持。参照だけクリア。
        self._repo = None

    def restart_worker(self, reason: str, force: bool = False) -> bool:
        return self._spawn_worker(reason=reason, force=force)

    def is_stalled(self, stall_sec: float = 60.0) -> bool:
        if self.queue_size <= 0:
            return False
        last = self._last_processed_at
        if last <= 0:
            return False
        return (time.monotonic() - last) >= stall_sec

    def get_debug_snapshot(self) -> dict:
        now = time.monotonic()
        last_processed_age = (now - self._last_processed_at) if self._last_processed_at > 0 else None
        last_feed_age = (now - self._last_feed_at) if self._last_feed_at > 0 else None
        # フロントで「初回ダウンロード中」「ロード中」「準備完了」を区別表示できるよう
        # state を 1 文字列にまとめる
        if self.model_error:
            model_state = "error"
        elif self.is_model_loaded:
            model_state = "ready"
        else:
            model_state = "loading"
        return {
            "model_loaded": self.is_model_loaded,
            "model_error": self.model_error,
            "model_state": model_state,
            "model_name": self.model_name,
            "queue_size": self.queue_size,
            "queue_maxsize": self._queue_maxsize,
            "total_segments": self.total_segments,
            "last_rms": round(self.last_rms, 4),
            "peak_rms": round(self.peak_rms, 4),
            "rms_threshold": round(self.rms_threshold, 4),
            "current_chunk_audio_sec": round(self.current_chunk_audio_sec, 2),
            "current_processing_sec": round(self.current_processing_sec, 2),
            "avg_speed_ratio": round(self.avg_speed_ratio, 2),
            "pending_audio_sec": round(self.pending_audio_sec, 2),
            "worker_alive": self.worker_alive,
            "worker_restarts": self.worker_restarts,
            "last_restart_reason": self._last_restart_reason,
            "last_restart_age_sec": round(now - self._last_restart_at, 2) if self._last_restart_at > 0 else None,
            "last_processed_age_sec": round(last_processed_age, 2) if last_processed_age is not None else None,
            "last_feed_age_sec": round(last_feed_age, 2) if last_feed_age is not None else None,
            "dropped_chunks": self.dropped_chunks,
            "dropped_audio_sec": round(self.dropped_audio_sec, 2),
            "transcribe_errors": self._transcribe_errors,
            "filtered_segments": self._filtered_segments,
        }

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _chunker_pending_sec(self) -> float:
        return estimate_chunker_pending_sec(self._chunker, sample_rate=SAMPLE_RATE)

    def _enqueue(self, chunk: np.ndarray) -> None:
        chunk_dur = len(chunk) / SAMPLE_RATE
        # 全 feed 済み audio から、chunker が保持している未確定末尾分を引くと、
        # 「今回確定した chunk の終了時刻」が得られる。そこから dur を引いて先頭時刻化。
        # これで smart-cut の tail 保持時も、無音ドロップ時も絶対時刻を維持できる。
        pending_after_emit = self._chunker_pending_sec()
        chunk_end = max(0.0, self._total_audio_sec - pending_after_emit)
        chunk_start = max(0.0, chunk_end - chunk_dur)
        # backlog が溜まり過ぎるとメモリ逼迫で全体停止しやすいので古い chunk を破棄する。
        while self.pending_audio_sec >= self._max_pending_audio_sec:
            if not self._drop_oldest_chunk("pending limit"):
                break

        payload = (chunk, chunk_start)
        try:
            self._queue.put_nowait(payload)
            with self._pending_lock:
                self._pending_audio_sec += chunk_dur
            return
        except queue.Full:
            pass

        if self._drop_oldest_chunk("queue full"):
            try:
                self._queue.put_nowait(payload)
                with self._pending_lock:
                    self._pending_audio_sec += chunk_dur
                return
            except queue.Full:
                pass

        # 最後の保険: 新規 chunk を捨てる
        self._mark_drop(chunk_dur, reason="drop new chunk")

    def _drop_oldest_chunk(self, reason: str) -> bool:
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return False

        if item is None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            return False

        old_chunk, _ = item
        old_dur = len(old_chunk) / SAMPLE_RATE
        self._mark_drop(old_dur, reason=reason)
        with self._pending_lock:
            self._pending_audio_sec = max(0.0, self._pending_audio_sec - old_dur)
        return True

    def _mark_drop(self, chunk_dur: float, reason: str) -> None:
        self._dropped_chunks += 1
        self._dropped_audio_sec += chunk_dur
        logger.warning("Streaming queue drop (%.2fs, reason=%s)", chunk_dur, reason)

    def _spawn_worker(self, reason: str, force: bool) -> bool:
        with self._worker_lock:
            if not self._running:
                return False
            if self._worker is not None and self._worker.is_alive() and not force:
                return False
            self._active_generation += 1
            generation = self._active_generation
            worker = threading.Thread(
                target=self._run_worker,
                args=(generation,),
                daemon=True,
                name=f"streaming-transcriber-{generation}",
            )
            self._worker = worker
            worker.start()
            if reason != "start":
                self._worker_restarts += 1
                self._last_restart_reason = reason
                self._last_restart_at = time.monotonic()
                logger.warning("Streaming worker restarted (reason=%s, gen=%d)", reason, generation)
            return True

    def _run_worker(self, generation: int) -> None:
        # 他アプリへの影響軽減のため少しだけ priority 下げる(snappy 優先)
        try:
            import os
            os.nice(3)
        except (OSError, AttributeError):
            pass

        try:
            self._repo = get_or_load_model(
                self.model_name,
                timeout_sec=self._model_load_timeout_sec,
            )
            self._model_loaded.set()
        except Exception as e:
            self._model_error = str(e)
            logger.error("Whisper load failed: %s", e)
            self._model_loaded.set()
            while True:
                if generation != self._active_generation:
                    return
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if not self._running:
                        return
                    continue
                except Exception as e2:
                    logger.error("Worker queue get error (load-failed loop): %s", e2)
                    time.sleep(0.5)
                    continue
                if item is None:
                    return

        # メインループ: 各イテレーションを try/except で囲い、何があっても worker を生かす
        self._last_processed_at = time.monotonic()
        while True:
            if generation != self._active_generation:
                logger.info("Streaming worker generation changed, exiting old worker (gen=%d)", generation)
                return
            try:
                try:
                    item = self._queue.get(timeout=1.0)
                except queue.Empty:
                    if not self._running and self._queue.empty():
                        break
                    continue
                if item is None:
                    break
                chunk_audio, chunk_start = item
                self._transcribe(chunk_audio, chunk_start, generation)
                self._last_processed_at = time.monotonic()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # 何があっても worker は止めない
                logger.error("Worker iteration error (will continue): %s", e, exc_info=True)
                time.sleep(0.2)
        logger.info("Streaming worker exited cleanly")

    def _transcribe(self, audio: np.ndarray, start_offset: float, generation: int) -> None:
        if self._repo is None:
            return
        chunk_dur = len(audio) / SAMPLE_RATE
        speaker_meta: dict | None = None
        is_active_generation = generation == self._active_generation
        if is_active_generation:
            self._current_chunk_audio_sec = chunk_dur
            self._current_chunk_started_at = time.monotonic()
        try:
            import mlx_whisper
            t0 = time.time()
            with self._recent_text_lock:
                recent_tail = self._recent_text_tail
            initial_prompt = build_initial_prompt(self._glossary, recent_tail)
            kwargs: dict = {
                "path_or_hf_repo": self._repo,
                "language": self.language,
                # word-level タイムスタンプ: 録音終了時の話者分離 (pyannote ターン境界)で
                # 単語単位の分割に使う。コスト +20% 程度。
                "word_timestamps": True,
                "verbose": False,
                # チャンク内の複数 segment をデコードする際、前の segment 出力を
                # 文脈として活用 (表記揺れを抑える)。drift リスクは compression_ratio /
                # logprob threshold で検出される。
                "condition_on_previous_text": True,
                # 単一温度で fallback decoding を無効化(高速化)
                "temperature": 0.0,
                # 再文字起こしと品質を揃える: 圧縮率/対数確率/無音判定/ハルシ抑制
                "compression_ratio_threshold": 2.4,
                "logprob_threshold": -1.0,
                "no_speech_threshold": 0.6,
                "hallucination_silence_threshold": 2.0,
            }
            if initial_prompt:
                kwargs["initial_prompt"] = initial_prompt
            result = mlx_whisper.transcribe(
                audio.astype(np.float32),
                **kwargs,
            )
            if generation != self._active_generation:
                logger.info(
                    "Discarding stale transcribe result after decode "
                    "(gen=%d, active=%d, audio=%.1fs)",
                    generation,
                    self._active_generation,
                    chunk_dur,
                )
                return
            try:
                speaker_meta = speaker_memory.identify(
                    audio,
                    SAMPLE_RATE,
                    session_id=self._session_id,
                    start_sec=start_offset,
                    end_sec=start_offset + chunk_dur,
                )
            except Exception as e:
                logger.warning("speaker identify failed: %s", e)
            if generation != self._active_generation:
                logger.info(
                    "Discarding stale transcribe result after speaker identify "
                    "(gen=%d, active=%d, audio=%.1fs)",
                    generation,
                    self._active_generation,
                    chunk_dur,
                )
                return
            new_segments: list[dict] = []
            filtered_segments = 0
            for seg in result.get("segments", []):
                text = (seg.get("text") or "").strip()
                if not text:
                    continue
                if self._hallucination_filter.is_hallucination(text):
                    filtered_segments += 1
                    logger.info("Filtered hallucination segment: %s", text[:80])
                    continue
                # corrections (wrong→correct) をリアルタイムに適用。要約後の post-process
                # を待たずに UI で正式表記を見せるため。
                if self._corrections:
                    for wrong, correct in self._corrections:
                        if wrong in text:
                            text = text.replace(wrong, correct)
                # words: 録音終了時の rediarize で話者ターン境界で分割するために使う
                words_raw = seg.get("words") or []
                words: list[dict] = []
                for w in words_raw:
                    wt = (w.get("word") or "").strip()
                    if not wt:
                        continue
                    try:
                        ws = float(w.get("start", 0))
                        we = float(w.get("end", 0))
                    except Exception:
                        continue
                    words.append({
                        "start": round(start_offset + ws, 3),
                        "end": round(start_offset + we, 3),
                        "word": wt,
                    })
                new_segments.append({
                    "start": round(start_offset + float(seg.get("start", 0.0)), 2),
                    "end": round(start_offset + float(seg.get("end", 0.0)), 2),
                    "text": text,
                    "words": words,
                    "speaker_id": (speaker_meta or {}).get("speaker_id"),
                    "speaker_label": (speaker_meta or {}).get("speaker_label"),
                    "speaker_confidence": (speaker_meta or {}).get("speaker_confidence"),
                })
            with self._worker_lock:
                if generation != self._active_generation:
                    logger.info(
                        "Discarding stale transcribe result before apply "
                        "(gen=%d, active=%d, audio=%.1fs, segments=%d)",
                        generation,
                        self._active_generation,
                        chunk_dur,
                        len(new_segments),
                    )
                    return
                self._filtered_segments += filtered_segments
                elapsed = time.time() - t0
                ratio = chunk_dur / max(elapsed, 1e-3)
                self._recent_ratios.append(ratio)
                if len(self._recent_ratios) > 6:
                    self._recent_ratios.pop(0)
                logger.info("Streaming transcribe: %.1fs audio → %d seg in %.1fs (%.1fx)",
                            chunk_dur, len(new_segments), elapsed, ratio)
                with self._segments_lock:
                    self._segments.extend(new_segments)
                if new_segments:
                    tail = " ".join(s.get("text", "") for s in new_segments[-3:]).strip()
                    if tail:
                        with self._recent_text_lock:
                            # 直近 N チャンクぶんを保持。max ~ PROMPT_RECENT_CHARS で十分
                            merged = (self._recent_text_tail + " " + tail).strip()
                            self._recent_text_tail = merged[-PROMPT_RECENT_CHARS:]
                for seg in new_segments:
                    self._emit(seg)
        except Exception as e:
            self._transcribe_errors += 1
            logger.error("Streaming transcribe failed: %s", e)
        finally:
            # 進捗トラッキングをリセット + pending を減算
            if is_active_generation and generation == self._active_generation:
                self._current_chunk_started_at = None
                self._current_chunk_audio_sec = 0.0
            with self._pending_lock:
                self._pending_audio_sec = max(0.0, self._pending_audio_sec - chunk_dur)

    def _emit(self, segment: dict) -> None:
        if self._on_segment is None or self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._on_segment(segment), self._loop)
        except RuntimeError:
            # ループが閉じている可能性
            pass
