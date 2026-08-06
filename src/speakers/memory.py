from __future__ import annotations

import logging
import threading
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import yaml

from src.config import APP_DIR, config
from src.security import resolve_path_under_base

logger = logging.getLogger(__name__)

SPEAKERS_PATH = APP_DIR / "speakers.yaml"
SAMPLE_DIR = APP_DIR / "speaker_samples"


def _resolve_app_relative_path(rel: str) -> Path | None:
    try:
        return resolve_path_under_base(APP_DIR, rel)
    except ValueError:
        logger.warning("Ignoring unsafe relative path in speakers store: %s", rel)
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 1e-8 or nb <= 1e-8:
        return -1.0
    return float(np.dot(a, b) / (na * nb))


def _kmeans_cosine(
    embeddings: np.ndarray,
    k: int,
    *,
    restarts: int = 8,
    max_iter: int = 30,
) -> tuple[np.ndarray, np.ndarray]:
    n = embeddings.shape[0]
    if n <= 0:
        raise ValueError("No embeddings")
    if k <= 1 or n <= k:
        labels = np.zeros(n, dtype=np.int32)
        center = np.mean(embeddings, axis=0, keepdims=True)
        norm = np.linalg.norm(center, axis=1, keepdims=True) + 1e-8
        return labels, center / norm

    best_labels: np.ndarray | None = None
    best_centers: np.ndarray | None = None
    best_inertia = float("inf")
    rng = np.random.default_rng()

    for _ in range(max(1, restarts)):
        seed_idx = rng.choice(n, size=k, replace=False)
        centers = embeddings[seed_idx].copy()

        for _it in range(max_iter):
            sims = embeddings @ centers.T
            labels = np.argmax(sims, axis=1)
            new_centers = centers.copy()
            for c in range(k):
                mask = labels == c
                if not np.any(mask):
                    new_centers[c] = embeddings[rng.integers(0, n)]
                    continue
                mean = np.mean(embeddings[mask], axis=0)
                norm = np.linalg.norm(mean) + 1e-8
                new_centers[c] = mean / norm
            if np.allclose(new_centers, centers, atol=1e-4):
                centers = new_centers
                break
            centers = new_centers

        sims = embeddings @ centers.T
        labels = np.argmax(sims, axis=1).astype(np.int32)
        inertia = float(np.mean(1.0 - sims[np.arange(n), labels]))
        if inertia < best_inertia:
            best_inertia = inertia
            best_labels = labels
            best_centers = centers

    assert best_labels is not None
    assert best_centers is not None
    return best_labels, best_centers


def _silhouette_cosine(embeddings: np.ndarray, labels: np.ndarray) -> float:
    n = embeddings.shape[0]
    if n <= 2:
        return 0.0
    unique = np.unique(labels)
    if unique.size <= 1:
        return 0.0

    sim = embeddings @ embeddings.T
    dist = 1.0 - sim
    score_sum = 0.0
    for i in range(n):
        own = labels[i]
        own_idx = np.where(labels == own)[0]
        if own_idx.size <= 1:
            continue
        own_wo_self = own_idx[own_idx != i]
        if own_wo_self.size == 0:
            continue
        a = float(np.mean(dist[i, own_wo_self]))
        b = float("inf")
        for c in unique:
            if c == own:
                continue
            idx = np.where(labels == c)[0]
            if idx.size == 0:
                continue
            b = min(b, float(np.mean(dist[i, idx])))
        if not np.isfinite(b):
            continue
        score_sum += (b - a) / max(a, b, 1e-8)

    return score_sum / n


def _extract_embedding(audio: np.ndarray, sample_rate: int) -> np.ndarray | None:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    min_audio_sec = float(
        config.get("whisper", "speaker_memory", "min_audio_sec", default=1.0)
    )
    min_samples = max(800, int(sample_rate * min_audio_sec))
    if x.size < min_samples:
        return None

    peak = float(np.max(np.abs(x)))
    if peak < 1e-5:
        return None
    x = x / (peak + 1e-8)

    n_fft = 512 if sample_rate >= 16000 else 256
    hop = max(64, int(sample_rate * 0.01))
    if x.size < n_fft:
        x = np.pad(x, (0, n_fft - x.size))

    frame_count = 1 + (x.size - n_fft) // hop
    if frame_count <= 0:
        return None

    idx = np.arange(n_fft)[None, :] + hop * np.arange(frame_count)[:, None]
    frames = x[idx]
    win = np.hanning(n_fft).astype(np.float32)
    frames = frames * win

    spec = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32) + 1e-8
    power = spec * spec
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate).astype(np.float32)
    nyquist = max(1.0, sample_rate / 2.0)

    pow_sum = np.sum(power, axis=1) + 1e-8
    centroid = np.sum(power * freqs[None, :], axis=1) / pow_sum
    bandwidth = np.sqrt(
        np.sum(power * (freqs[None, :] - centroid[:, None]) ** 2, axis=1) / pow_sum
    )
    rolloff_pos = np.argmax(np.cumsum(power, axis=1) >= (pow_sum * 0.85)[:, None], axis=1)
    rolloff = freqs[rolloff_pos]
    zcr = np.mean(np.abs(np.diff(np.signbit(frames), axis=1)), axis=1)
    energy = np.log(np.mean(frames ** 2, axis=1) + 1e-8)

    # 粗い帯域エネルギーを特徴量として加える
    band_edges = np.linspace(0, power.shape[1] - 1, 9, dtype=int)
    band_mean: list[float] = []
    band_std: list[float] = []
    for i in range(8):
        start = int(band_edges[i])
        end = int(band_edges[i + 1])
        if end <= start:
            end = start + 1
        band = np.log(np.mean(power[:, start:end], axis=1) + 1e-8)
        band_mean.append(float(np.mean(band)))
        band_std.append(float(np.std(band)))

    feat = np.array(
        [
            float(np.mean(centroid) / nyquist),
            float(np.std(centroid) / nyquist),
            float(np.mean(bandwidth) / nyquist),
            float(np.std(bandwidth) / nyquist),
            float(np.mean(rolloff) / nyquist),
            float(np.std(rolloff) / nyquist),
            float(np.mean(zcr)),
            float(np.std(zcr)),
            float(np.mean(energy)),
            float(np.std(energy)),
            *band_mean,
            *band_std,
        ],
        dtype=np.float32,
    )

    feat = (feat - float(np.mean(feat))) / (float(np.std(feat)) + 1e-8)
    norm = float(np.linalg.norm(feat))
    if norm <= 1e-8:
        return None
    return feat / norm


def _smooth_turns(turns: list[dict], *, min_dur: float = 0.5) -> list[dict]:
    """pyannote のターンをスムージング:
    - 同じ話者の連続ターンを結合
    - min_dur 未満の短いターンは前後の話者に吸収 (誤検出のジッターを除去)
    """
    if not turns:
        return []
    items = sorted(
        ({"start": float(t["start"]), "end": float(t["end"]), "speaker": str(t["speaker"])}
         for t in turns),
        key=lambda t: t["start"],
    )
    # 同一話者の隣接ターンを結合
    merged: list[dict] = []
    for t in items:
        if merged and merged[-1]["speaker"] == t["speaker"] and t["start"] <= merged[-1]["end"] + 0.2:
            merged[-1]["end"] = max(merged[-1]["end"], t["end"])
        else:
            merged.append(dict(t))

    # 短すぎるターンを吸収
    cleaned: list[dict] = []
    for i, t in enumerate(merged):
        dur = t["end"] - t["start"]
        if dur < min_dur and (cleaned or i + 1 < len(merged)):
            # 隣接ターンが長い方に統合
            prev_t = cleaned[-1] if cleaned else None
            next_t = merged[i + 1] if i + 1 < len(merged) else None
            target = None
            if prev_t and next_t:
                target = prev_t if (prev_t["end"] - prev_t["start"]) >= (next_t["end"] - next_t["start"]) else next_t
            elif prev_t:
                target = prev_t
            elif next_t:
                target = next_t
            if target is prev_t:
                target["end"] = max(target["end"], t["end"])
            elif target is next_t:
                target["start"] = min(target["start"], t["start"])
            else:
                cleaned.append(t)
        else:
            cleaned.append(t)

    # 結合後にもう一度同一話者の隣接を圧縮
    final: list[dict] = []
    for t in cleaned:
        if final and final[-1]["speaker"] == t["speaker"] and t["start"] <= final[-1]["end"] + 0.2:
            final[-1]["end"] = max(final[-1]["end"], t["end"])
        else:
            final.append(dict(t))
    return final


def _coalesce_subsegments(subs: list[dict], *, gap: float = 0.4, min_chars: int = 3) -> list[dict]:
    """分割後の細切れを話者単位で再マージ。

    - 同じ話者で gap 秒以下しか空いていなければ結合
    - min_chars 未満の短いテキストは前(同話者)に追記、無理なら次の同話者に追記
    """
    if not subs:
        return []
    out: list[dict] = []
    for s in subs:
        if not out:
            out.append(dict(s))
            continue
        last = out[-1]
        same_speaker = last.get("_speaker_local") == s.get("_speaker_local")
        if same_speaker and float(s.get("start", 0)) - float(last.get("end", 0)) <= gap:
            last["end"] = max(float(last["end"]), float(s["end"]))
            last["text"] = (str(last.get("text", "")) + str(s.get("text", ""))).strip()
        else:
            out.append(dict(s))
    # 短すぎるテキストを処理: 同話者の前後に吸収できるならする、できなければそのまま
    cleaned: list[dict] = []
    for s in out:
        text = str(s.get("text", "")).strip()
        if len(text) < min_chars and cleaned and cleaned[-1].get("_speaker_local") == s.get("_speaker_local"):
            cleaned[-1]["end"] = max(float(cleaned[-1]["end"]), float(s["end"]))
            cleaned[-1]["text"] = (str(cleaned[-1].get("text", "")) + text).strip()
        else:
            cleaned.append(s)
    return cleaned


def _split_segment_by_turns(seg: dict, turns: list[dict]) -> list[dict]:
    """1つの whisper セグメントが複数の pyannote ターンを跨ぐ場合に分割する。

    word_timestamps があればそれを使って word 単位で分割。
    なければ time-proportion で分割 (テキストは時間比で割る)。
    返り値の各 sub-segment は `_speaker_local` で対応する pyannote 話者を持つ。
    """
    try:
        s_start = float(seg.get("start", 0.0))
        s_end = float(seg.get("end", s_start))
    except Exception:
        return [dict(seg)]
    if s_end <= s_start:
        return [dict(seg)]

    # 重なるターンを抽出 (overlap > 0)
    overlapping = []
    for t in turns:
        ov = max(0.0, min(s_end, t["end"]) - max(s_start, t["start"]))
        if ov > 0:
            overlapping.append((t, ov))
    if not overlapping:
        return [dict(seg)]
    if len(overlapping) == 1:
        cloned = dict(seg)
        cloned["_speaker_local"] = str(overlapping[0][0]["speaker"])
        return [cloned]

    text = str(seg.get("text") or "").strip()
    words = seg.get("words") or []

    # word-level 分割
    if words:
        groups: list[tuple[str, list[dict]]] = []  # [(speaker_local, [word, ...]), ...]
        for w in words:
            try:
                ws = float(w.get("start", s_start))
                we = float(w.get("end", ws))
            except Exception:
                continue
            wmid = (ws + we) / 2
            # word の中央が含まれるターンに割り振る (なければ最大 overlap)
            best_t = None
            best_ov = -1.0
            for t, _ in overlapping:
                if t["start"] <= wmid < t["end"]:
                    best_t = t
                    break
                ov = max(0.0, min(we, t["end"]) - max(ws, t["start"]))
                if ov > best_ov:
                    best_ov = ov
                    best_t = t
            if best_t is None:
                continue
            spk = str(best_t["speaker"])
            if groups and groups[-1][0] == spk:
                groups[-1][1].append(w)
            else:
                groups.append((spk, [w]))

        if not groups:
            cloned = dict(seg)
            cloned["_speaker_local"] = str(max(overlapping, key=lambda x: x[1])[0]["speaker"])
            return [cloned]

        # 1グループしか出来なかったら分割不要
        if len(groups) == 1:
            cloned = dict(seg)
            cloned["_speaker_local"] = groups[0][0]
            return [cloned]

        # 連結した結果がオリジナルテキストと食い違わないよう、word.word を直結
        out: list[dict] = []
        for spk, ws_group in groups:
            joined = "".join(str(w.get("word", "")) for w in ws_group).strip()
            if not joined:
                continue
            seg_start = round(float(ws_group[0].get("start", s_start)), 2)
            seg_end = round(float(ws_group[-1].get("end", s_end)), 2)
            if seg_end <= seg_start:
                seg_end = seg_start + 0.01
            out.append({
                "start": seg_start,
                "end": seg_end,
                "text": joined,
                "_speaker_local": spk,
            })
        return out if out else [dict(seg)]

    # time-proportion フォールバック (words 無し)
    # ターンを開始時刻でソート
    sorted_turns = sorted([t for t, _ in overlapping], key=lambda x: x["start"])
    total_dur = s_end - s_start
    out: list[dict] = []
    text_pos = 0
    for idx, t in enumerate(sorted_turns):
        sub_start = max(s_start, t["start"])
        sub_end = min(s_end, t["end"]) if idx < len(sorted_turns) - 1 else s_end
        if sub_end <= sub_start:
            continue
        ratio_end = (sub_end - s_start) / total_dur
        text_end = int(round(len(text) * ratio_end)) if idx < len(sorted_turns) - 1 else len(text)
        sub_text = text[text_pos:text_end].strip()
        text_pos = text_end
        if not sub_text:
            continue
        out.append({
            "start": round(sub_start, 2),
            "end": round(sub_end, 2),
            "text": sub_text,
            "_speaker_local": str(t["speaker"]),
        })
    return out if out else [dict(seg)]


class SpeakerMemory:
    def __init__(self) -> None:
        self._path = SPEAKERS_PATH
        self._sample_dir = SAMPLE_DIR
        self._lock = threading.Lock()
        # label map のキャッシュ (yaml の mtime 変化で自動再読込)
        self._label_cache: dict[str, str] | None = None
        self._label_cache_mtime: float = 0.0
        self._ensure_store()

    def _ensure_store(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._sample_dir.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            return
        self._save_raw({"schema_version": 1, "speakers": []})

    def _load_raw(self) -> dict[str, Any]:
        with open(self._path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        speakers = raw.get("speakers")
        if not isinstance(speakers, list):
            raw["speakers"] = []
        raw.setdefault("schema_version", 1)
        return raw

    def _save_raw(self, data: dict[str, Any]) -> None:
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def _next_auto_index(self, speakers: list[dict[str, Any]]) -> int:
        max_idx = 0
        for sp in speakers:
            idx = sp.get("auto_index")
            if isinstance(idx, int):
                max_idx = max(max_idx, idx)
                continue
            label = str(sp.get("label", ""))
            if label.startswith("話者"):
                suffix = label.removeprefix("話者")
                if suffix.isdigit():
                    max_idx = max(max_idx, int(suffix))
        return max_idx + 1

    def _write_sample_wav(self, speaker_id: str, audio: np.ndarray, sample_rate: int) -> str | None:
        sample_sec = float(
            config.get("whisper", "speaker_memory", "sample_sec", default=2.5)
        )
        sample_count = max(800, int(sample_sec * sample_rate))
        clip = np.asarray(audio, dtype=np.float32).reshape(-1)[:sample_count]
        if clip.size == 0:
            return None
        clip = np.clip(clip, -1.0, 1.0)
        pcm = (clip * 32767.0).astype(np.int16)
        rel = f"speaker_samples/{speaker_id}.wav"
        out = APP_DIR / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with wave.open(str(out), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm.tobytes())
            return rel
        except Exception as e:
            logger.warning("Failed to write speaker sample (%s): %s", speaker_id, e)
            return None

    def _load_wav_mono_f32(self, wav_path: str | Path) -> tuple[np.ndarray, int] | None:
        path = Path(wav_path)
        ext = path.suffix.lower()
        if ext == ".wav":
            try:
                with wave.open(str(path), "rb") as wf:
                    sr = int(wf.getframerate())
                    ch = int(wf.getnchannels())
                    n = int(wf.getnframes())
                    raw = wf.readframes(n)
            except Exception as e:
                logger.warning("Failed to read wav for diarization (%s): %s", path, e)
                return None
            if not raw:
                return None
            data = np.frombuffer(raw, dtype=np.int16)
            if ch > 1:
                data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
            return data.astype(np.float32) / 32768.0, sr

        # FLAC など: ffmpeg で 16kHz mono f32le にデコード(埋め込み抽出には十分)
        try:
            import subprocess
            from src.audio.recorder import FFMPEG
            proc = subprocess.run(
                [FFMPEG, "-loglevel", "error", "-i", str(path),
                 "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                capture_output=True, timeout=600,
            )
            if proc.returncode != 0:
                logger.warning("ffmpeg decode failed for diarization (%s): %s",
                               path, proc.stderr[-500:])
                return None
            audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
            if audio.size == 0:
                return None
            return audio, 16000
        except Exception as e:
            logger.warning("Failed to decode audio for diarization (%s): %s", path, e)
            return None

    def _choose_cluster_count(self, embeddings: np.ndarray) -> tuple[int, np.ndarray]:
        n = embeddings.shape[0]
        if n < 8:
            return 1, np.zeros(n, dtype=np.int32)

        k_max = min(6, max(2, int(n / 35) + 2))
        scored: list[tuple[int, float, np.ndarray]] = []
        for k in range(2, k_max + 1):
            labels, _ = _kmeans_cosine(embeddings, k)
            score = _silhouette_cosine(embeddings, labels)
            scored.append((k, score, labels))

        if not scored:
            return 1, np.zeros(n, dtype=np.int32)

        best_score = max(item[1] for item in scored)
        # 過少分離を避けるため、best に近いスコアなら多めの k を採用。
        keep_ratio = float(
            config.get("whisper", "speaker_memory", "rediarize_keep_ratio", default=0.86)
        )
        keep_ratio = max(0.6, min(0.99, keep_ratio))
        candidates = [item for item in scored if item[1] >= best_score * keep_ratio]
        chosen = max(candidates, key=lambda item: item[0]) if candidates else max(scored, key=lambda item: item[1])
        return chosen[0], chosen[2].astype(np.int32)

    def _reassign_tiny_clusters(self, embeddings: np.ndarray, labels: np.ndarray) -> np.ndarray:
        if labels.size == 0:
            return labels
        n = labels.size
        min_cluster_size = max(2, int(n * 0.03))
        unique = np.unique(labels)
        counts = {int(c): int(np.sum(labels == c)) for c in unique}
        major = [c for c, cnt in counts.items() if cnt >= min_cluster_size]
        if not major:
            return labels

        centers = {}
        for c in major:
            mean = np.mean(embeddings[labels == c], axis=0)
            centers[c] = mean / (np.linalg.norm(mean) + 1e-8)

        out = labels.copy()
        for i, c in enumerate(labels):
            if counts[int(c)] >= min_cluster_size:
                continue
            best_c = int(c)
            best_sim = -1.0
            for m in major:
                sim = _cosine_similarity(embeddings[i], centers[m])
                if sim > best_sim:
                    best_sim = sim
                    best_c = int(m)
            out[i] = best_c
        return out

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            raw = self._load_raw()
            speakers = raw.get("speakers", [])
            items: list[dict[str, Any]] = []
            for sp in speakers:
                d = {
                    "id": str(sp.get("id", "")),
                    "label": str(sp.get("label", "")),
                    "appearances": int(sp.get("appearances", 0)),
                    "total_audio_sec": round(float(sp.get("total_audio_sec", 0.0)), 1),
                    "created_at": str(sp.get("created_at", "")),
                    "updated_at": str(sp.get("updated_at", "")),
                    "sample_available": False,
                    "sample_session_id": sp.get("sample_session_id"),
                }
                rel = sp.get("sample_wav")
                if isinstance(rel, str) and rel:
                    path = _resolve_app_relative_path(rel)
                    d["sample_available"] = bool(path and path.exists())
                items.append(d)
            items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
            return items

    def rename(self, speaker_id: str, label: str) -> dict[str, Any] | None:
        cleaned = label.strip()
        if not cleaned:
            return None
        with self._lock:
            raw = self._load_raw()
            speakers = raw.get("speakers", [])
            for sp in speakers:
                if sp.get("id") != speaker_id:
                    continue
                sp["label"] = cleaned
                sp["updated_at"] = _now_iso()
                self._save_raw(raw)
                return {
                    "id": speaker_id,
                    "label": cleaned,
                    "updated_at": sp["updated_at"],
                }
        return None

    def delete(self, speaker_id: str) -> dict[str, Any] | None:
        """話者プロファイルを削除。サンプル wav も削除。

        DB 側のセグメント参照は呼び出し側 (API) で reassign_speaker_id(source, None) する。
        """
        with self._lock:
            raw = self._load_raw()
            speakers = raw.get("speakers", [])
            target = None
            for i, sp in enumerate(speakers):
                if sp.get("id") == speaker_id:
                    target = speakers.pop(i)
                    break
            if target is None:
                return None
            # サンプル wav を削除 (best-effort)
            rel = target.get("sample_wav")
            if isinstance(rel, str) and rel:
                try:
                    p = _resolve_app_relative_path(rel)
                    if p and p.exists():
                        p.unlink()
                except Exception as e:
                    logger.warning("Failed to remove sample wav for %s: %s", speaker_id, e)
            raw["speakers"] = speakers
            self._save_raw(raw)
            return {"id": str(target.get("id")), "label": str(target.get("label", ""))}

    def merge(self, primary_id: str, source_ids: list[str]) -> dict[str, Any] | None:
        """source_ids のプロファイルを primary_id に統合する。

        - appearances / total_audio_sec を合算
        - embedding を appearances 重み付け平均
        - source プロファイルは yaml から削除
        - サンプル wav は primary を優先、なければ最初の有効 source を採用
        - DB 側の speaker_id 書き換えは呼び出し側で行う

        戻り値: { primary: profile_dict, merged: [profile_dict, ...] } or None
        """
        if not source_ids:
            return None
        with self._lock:
            raw = self._load_raw()
            speakers: list[dict[str, Any]] = raw.get("speakers", [])

            primary = next((sp for sp in speakers if sp.get("id") == primary_id), None)
            if primary is None:
                return None

            sources: list[dict[str, Any]] = []
            for sid in source_ids:
                if sid == primary_id:
                    continue
                sp = next((s for s in speakers if s.get("id") == sid), None)
                if sp is not None:
                    sources.append(sp)
            if not sources:
                return {"primary": primary, "merged": []}

            # embedding を appearances 重み付け平均で合成
            try:
                p_emb = np.asarray(primary.get("embedding") or [], dtype=np.float32)
            except Exception:
                p_emb = np.zeros(0, dtype=np.float32)
            p_app = float(max(1, int(primary.get("appearances", 0))))
            mixed = p_emb.copy() if p_emb.size > 0 else None
            mixed_w = p_app if mixed is not None else 0.0

            for src in sources:
                try:
                    s_emb = np.asarray(src.get("embedding") or [], dtype=np.float32)
                except Exception:
                    continue
                s_app = float(max(1, int(src.get("appearances", 0))))
                if s_emb.size == 0:
                    continue
                if mixed is None or mixed.size == 0:
                    mixed = s_emb.copy()
                    mixed_w = s_app
                    continue
                if s_emb.size != mixed.size:
                    continue  # 次元違いはスキップ
                mixed = (mixed * mixed_w + s_emb * s_app) / (mixed_w + s_app)
                mixed_w += s_app

            if mixed is not None and mixed.size > 0:
                norm = float(np.linalg.norm(mixed))
                if norm > 1e-8:
                    mixed = mixed / norm
                primary["embedding"] = mixed.tolist()

            # 統計値を合算
            primary["appearances"] = int(primary.get("appearances", 0)) + sum(
                int(s.get("appearances", 0)) for s in sources
            )
            primary["total_audio_sec"] = float(primary.get("total_audio_sec", 0.0)) + sum(
                float(s.get("total_audio_sec", 0.0)) for s in sources
            )
            primary["updated_at"] = _now_iso()

            # サンプル wav: primary 優先、なければ source の有効なものを引き継ぎ
            if not primary.get("sample_wav"):
                for src in sources:
                    if src.get("sample_wav"):
                        primary["sample_wav"] = src["sample_wav"]
                        primary["sample_session_id"] = src.get("sample_session_id")
                        break

            # source を削除 + サンプル wav 掃除
            removed_ids = set()
            for src in sources:
                removed_ids.add(src.get("id"))
                rel = src.get("sample_wav")
                # primary に引き継いだ場合は消さない
                if isinstance(rel, str) and rel and rel != primary.get("sample_wav"):
                    try:
                        p = _resolve_app_relative_path(rel)
                        if p and p.exists():
                            p.unlink()
                    except Exception as e:
                        logger.warning("Failed to remove merged sample wav: %s", e)
            speakers = [sp for sp in speakers if sp.get("id") not in removed_ids]
            raw["speakers"] = speakers
            self._save_raw(raw)
            return {
                "primary": dict(primary),
                "merged": [{"id": s.get("id"), "label": s.get("label")} for s in sources],
            }

    def get_sample_path(self, speaker_id: str) -> Path | None:
        with self._lock:
            raw = self._load_raw()
            for sp in raw.get("speakers", []):
                if sp.get("id") != speaker_id:
                    continue
                rel = sp.get("sample_wav")
                if isinstance(rel, str) and rel:
                    path = _resolve_app_relative_path(rel)
                    if path and path.exists():
                        return path
                return None
        return None

    def get_label_map(self) -> dict[str, str]:
        # speakers.yaml の mtime をチェックして、変わってなければキャッシュを返す
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            mtime = 0.0
        with self._lock:
            if (
                self._label_cache is not None
                and mtime == self._label_cache_mtime
            ):
                return self._label_cache
            raw = self._load_raw()
            mapping: dict[str, str] = {}
            for sp in raw.get("speakers", []):
                sid = str(sp.get("id", ""))
                label = str(sp.get("label", "")).strip()
                if sid and label:
                    mapping[sid] = label
            self._label_cache = mapping
            self._label_cache_mtime = mtime
            return mapping

    def apply_latest_labels(self, segments: list[dict]) -> list[dict]:
        mapping = self.get_label_map()
        if not mapping:
            return segments
        changed = False
        out: list[dict] = []
        for seg in segments:
            sid = str(seg.get("speaker_id", "")).strip()
            if sid and sid in mapping and seg.get("speaker_label") != mapping[sid]:
                cloned = dict(seg)
                cloned["speaker_label"] = mapping[sid]
                out.append(cloned)
                changed = True
            else:
                out.append(seg)
        return out if changed else segments

    def rediarize_segments(
        self,
        segments: list[dict],
        *,
        wav_path: str | Path | None,
        session_id: str | None,
        on_progress: Callable[[str, float, str | None], None] | None = None,
    ) -> list[dict]:
        """セッション全体を見て話者を再割り当てする。

        ライブ時の逐次判定は取りこぼしが出やすいため、録音終了時に
        全セグメントの埋め込みをクラスタリングして補正する。

        on_progress(stage_key, progress 0..1, message) は時間のかかるフェーズ中に
        UI 用の進捗を出す。stage_key は "extract" | "cluster" | "assign"。
        """

        def _emit(stage: str, prog: float, msg: str | None = None) -> None:
            if on_progress is None:
                return
            try:
                on_progress(stage, max(0.0, min(1.0, prog)), msg)
            except Exception:
                pass

        diarization_enabled = bool(
            config.get("whisper", "speaker_memory", "diarization_enabled", default=True)
        )
        if not diarization_enabled:
            return segments

        enabled = bool(
            config.get("whisper", "speaker_memory", "enabled", default=True)
        )
        if not enabled or not segments or not wav_path:
            return self.apply_latest_labels(segments)

        provider = str(
            config.get("whisper", "speaker_memory", "diarization_provider", default="legacy")
        ).lower()

        if provider == "pyannote":
            try:
                return self._rediarize_pyannote(
                    segments, wav_path=wav_path, session_id=session_id,
                    on_progress=on_progress,
                )
            except Exception as e:
                logger.warning(
                    "pyannote rediarize failed, falling back to legacy: %s", e,
                )
                # フォールスルーして legacy を実行

        loaded = self._load_wav_mono_f32(wav_path)
        if loaded is None:
            return self.apply_latest_labels(segments)
        audio, sample_rate = loaded

        valid_indices: list[int] = []
        embeddings: list[np.ndarray] = []
        seg_audio_cache: dict[int, np.ndarray] = {}

        total = max(1, len(segments))
        _emit("extract", 0.0, f"話者特徴を抽出中... 0/{total}")
        for i, seg in enumerate(segments):
            try:
                start_sec = float(seg.get("start", 0.0))
                end_sec = float(seg.get("end", 0.0))
            except Exception:
                continue
            if end_sec <= start_sec:
                continue
            s0 = max(0, int(start_sec * sample_rate))
            s1 = min(len(audio), int(end_sec * sample_rate))
            if s1 <= s0:
                continue
            clip = audio[s0:s1]
            emb = _extract_embedding(clip, sample_rate)
            if emb is None:
                continue
            valid_indices.append(i)
            embeddings.append(emb)
            seg_audio_cache[i] = clip
            # 5 セグメントごと、または最後で進捗通知 (WS スパム抑制)
            if (i + 1) % 5 == 0 or (i + 1) == total:
                _emit(
                    "extract", (i + 1) / total,
                    f"話者特徴を抽出中... {i + 1}/{total}",
                )

        if len(embeddings) < 4:
            return self.apply_latest_labels(segments)

        _emit("cluster", 0.0, "話者クラスタリング中...")
        emb_mat = np.stack(embeddings, axis=0).astype(np.float32)
        _, labels = self._choose_cluster_count(emb_mat)
        labels = self._reassign_tiny_clusters(emb_mat, labels)
        _emit("cluster", 1.0, "話者クラスタリング完了")

        unique_labels = sorted(int(c) for c in np.unique(labels))
        cluster_first_idx: dict[int, int] = {}
        for pos, c in enumerate(labels):
            ci = int(c)
            if ci not in cluster_first_idx:
                cluster_first_idx[ci] = pos
        unique_labels.sort(key=lambda c: cluster_first_idx.get(c, 0))

        cluster_centroids: dict[int, np.ndarray] = {}
        cluster_rep_idx: dict[int, int] = {}
        for c in unique_labels:
            mask = labels == c
            centroid = np.mean(emb_mat[mask], axis=0)
            centroid = centroid / (np.linalg.norm(centroid) + 1e-8)
            cluster_centroids[c] = centroid
            # 代表サンプルは最長セグメントを採用
            rep_global_idx = valid_indices[int(np.where(mask)[0][0])]
            best_dur = -1.0
            for local_i in np.where(mask)[0]:
                global_i = valid_indices[int(local_i)]
                seg = segments[global_i]
                dur = float(seg.get("end", 0.0)) - float(seg.get("start", 0.0))
                if dur > best_dur:
                    best_dur = dur
                    rep_global_idx = global_i
            cluster_rep_idx[c] = rep_global_idx

        cluster_to_profile: dict[int, tuple[str, str, float]] = {}
        with self._lock:
            raw = self._load_raw()
            speakers: list[dict[str, Any]] = raw.get("speakers", [])
            now = _now_iso()
            used_speaker_ids: set[str] = set()
            merge_threshold = float(
                config.get("whisper", "speaker_memory", "global_match_threshold", default=0.992)
            )
            merge_threshold = max(0.7, min(0.999, merge_threshold))

            for c in unique_labels:
                centroid = cluster_centroids[c]
                best_sp: dict[str, Any] | None = None
                best_score = -1.0
                for sp in speakers:
                    sid = str(sp.get("id", ""))
                    if not sid or sid in used_speaker_ids:
                        continue
                    vec = sp.get("embedding")
                    if not isinstance(vec, list) or not vec:
                        continue
                    try:
                        old = np.asarray(vec, dtype=np.float32)
                    except Exception:
                        continue
                    if old.size != centroid.size:
                        continue
                    score = _cosine_similarity(centroid, old)
                    if score > best_score:
                        best_score = score
                        best_sp = sp

                if best_sp is not None and best_score >= merge_threshold:
                    sid = str(best_sp.get("id"))
                    label = str(best_sp.get("label") or sid)
                    used_speaker_ids.add(sid)
                    appearances = int(best_sp.get("appearances", 0))
                    weight = float(min(200, max(1, appearances)))
                    old = np.asarray(best_sp.get("embedding"), dtype=np.float32)
                    mixed = ((old * weight) + centroid) / (weight + 1.0)
                    mixed = mixed / (np.linalg.norm(mixed) + 1e-8)
                    best_sp["embedding"] = mixed.tolist()
                    best_sp["appearances"] = appearances + int(np.sum(labels == c))
                    best_sp["updated_at"] = now
                    cluster_to_profile[c] = (sid, label, round(best_score, 3))
                    continue

                auto_index = self._next_auto_index(speakers)
                sid = f"spk_{uuid.uuid4().hex[:8]}"
                label = f"話者{auto_index}"
                rep_idx = cluster_rep_idx[c]
                rep_audio = seg_audio_cache.get(rep_idx)
                sample_rel = self._write_sample_wav(sid, rep_audio, sample_rate) if rep_audio is not None else None
                total_audio_sec = 0.0
                for local_i, lbl in enumerate(labels):
                    if int(lbl) != c:
                        continue
                    seg = segments[valid_indices[local_i]]
                    total_audio_sec += max(0.1, float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)))
                speakers.append(
                    {
                        "id": sid,
                        "label": label,
                        "auto_index": auto_index,
                        "embedding": centroid.tolist(),
                        "appearances": int(np.sum(labels == c)),
                        "total_audio_sec": total_audio_sec,
                        "sample_wav": sample_rel,
                        "sample_session_id": session_id,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                used_speaker_ids.add(sid)
                cluster_to_profile[c] = (sid, label, 1.0)

            raw["speakers"] = speakers
            self._save_raw(raw)

        _emit("assign", 0.0, "話者ラベルを割り当て中...")
        out = [dict(seg) for seg in segments]
        prev_sid: str | None = None
        prev_label: str | None = None
        prev_conf: float | None = None

        local_to_cluster = {local_i: int(labels[local_i]) for local_i in range(len(valid_indices))}
        global_index_to_local = {
            global_idx: local_idx for local_idx, global_idx in enumerate(valid_indices)
        }

        for g_idx, seg in enumerate(out):
            local_i = global_index_to_local.get(g_idx)
            if local_i is None:
                if prev_sid and prev_label:
                    seg["speaker_id"] = prev_sid
                    seg["speaker_label"] = prev_label
                    seg["speaker_confidence"] = prev_conf
                continue
            cluster_id = local_to_cluster[local_i]
            sid, label, conf = cluster_to_profile[cluster_id]
            seg["speaker_id"] = sid
            seg["speaker_label"] = label
            seg["speaker_confidence"] = conf
            prev_sid, prev_label, prev_conf = sid, label, conf

        _emit("assign", 1.0, "話者割り当て完了")
        return self.apply_latest_labels(out)

    def _rediarize_pyannote(
        self,
        segments: list[dict],
        *,
        wav_path: str | Path,
        session_id: str | None,
        on_progress: Callable[[str, float, str | None], None] | None = None,
    ) -> list[dict]:
        """pyannote.audio を使ってセッション全体を再 diarize する。"""
        from src.speakers import pyannote_runner

        def _emit(stage: str, prog: float, msg: str | None = None) -> None:
            if on_progress is None:
                return
            try:
                on_progress(stage, max(0.0, min(1.0, prog)), msg)
            except Exception:
                pass

        _emit("extract", 0.0, "pyannote で話者分離中...")

        def _on_pyannote_progress(prog: float, msg: str | None) -> None:
            # pyannote 内部 (segmentation → embedding → clustering) の進捗を
            # extract ステージに 0..0.95 でマップ。残りはこの呼び出し後の
            # 集約/分割/ラベル付与処理に充てる。
            _emit("extract", min(0.95, max(0.0, prog)), msg)

        turns, centroids = pyannote_runner.diarize_with_embeddings(
            wav_path, on_progress=_on_pyannote_progress,
        )
        if not turns:
            logger.info("pyannote returned no turns, falling back to legacy")
            raise RuntimeError("pyannote returned empty diarization")
        _emit("cluster", 0.5, "話者ターンを集約中...")

        # ターン境界をスムージング (短すぎる誤検出ターンを吸収)
        smoothed_turns = _smooth_turns(turns, min_dur=0.5)
        logger.info(
            "[pyannote] turns %d → %d after smoothing", len(turns), len(smoothed_turns),
        )
        # スムージングで消えた話者の centroid は除外
        active_speakers = {t["speaker"] for t in smoothed_turns}
        centroids = {k: v for k, v in centroids.items() if k in active_speakers}
        turns = smoothed_turns

        # ターン境界でセグメントを再分割 (連続発話で1チャンクになった話者を分離)
        # words フィールドがあれば word-level、なければ time-proportion で分割
        split_segments: list[dict] = []
        for seg in segments:
            for sub in _split_segment_by_turns(seg, smoothed_turns):
                split_segments.append(sub)
        # 同話者の細切れを再マージ
        split_segments = _coalesce_subsegments(split_segments, gap=0.4, min_chars=3)
        logger.info(
            "[pyannote] segments %d → %d after split + coalesce",
            len(segments), len(split_segments),
        )

        # 各 split セグメントに speaker_local が付いている前提で seg_to_local を構築
        seg_to_local: dict[int, str] = {}
        for i, seg in enumerate(split_segments):
            local = seg.pop("_speaker_local", None)
            if local is not None:
                seg_to_local[i] = str(local)
                continue
            # フォールバック: 最大重なりで決める
            try:
                s_start = float(seg.get("start", 0.0))
                s_end = float(seg.get("end", s_start))
            except Exception:
                continue
            if s_end <= s_start:
                continue
            best_overlap = 0.0
            best_local: str | None = None
            for t in turns:
                overlap = max(0.0, min(s_end, t["end"]) - max(s_start, t["start"]))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_local = t["speaker"]
            if best_local is not None:
                seg_to_local[i] = best_local

        segments = split_segments  # 以後はこの分割済みリストを使う

        # 音声サンプル抽出用 (新規プロファイルのサンプル wav 用)
        loaded = self._load_wav_mono_f32(wav_path)

        local_to_profile: dict[str, tuple[str, str, float]] = {}
        threshold = float(
            config.get("whisper", "speaker_memory", "pyannote_match_threshold", default=0.7)
        )
        threshold = max(0.3, min(0.95, threshold))

        with self._lock:
            raw = self._load_raw()
            speakers: list[dict[str, Any]] = raw.get("speakers", [])
            now = _now_iso()
            used: set[str] = set()

            # ローカル speaker を順番に処理 (centroid がある場合のみマッチング可能)
            for local_speaker in centroids.keys():
                centroid = centroids[local_speaker]
                best_sp: dict[str, Any] | None = None
                best_score = -1.0
                for sp in speakers:
                    sid = str(sp.get("id", ""))
                    if not sid or sid in used:
                        continue
                    if str(sp.get("embedding_provider", "legacy")) != "pyannote":
                        continue
                    vec = sp.get("embedding")
                    if not isinstance(vec, list) or not vec:
                        continue
                    try:
                        old = np.asarray(vec, dtype=np.float32)
                    except Exception:
                        continue
                    if old.size != centroid.size:
                        continue
                    score = _cosine_similarity(centroid, old)
                    if score > best_score:
                        best_score = score
                        best_sp = sp

                turn_count = sum(
                    1 for _idx, lbl in seg_to_local.items() if lbl == local_speaker
                )
                total_audio_sec = sum(
                    max(0.0, t["end"] - t["start"])
                    for t in turns if t["speaker"] == local_speaker
                )

                if best_sp is not None and best_score >= threshold:
                    sid = str(best_sp.get("id"))
                    label = str(best_sp.get("label") or sid)
                    used.add(sid)
                    appearances = int(best_sp.get("appearances", 0))
                    weight = float(min(200, max(1, appearances)))
                    old = np.asarray(best_sp.get("embedding"), dtype=np.float32)
                    mixed = ((old * weight) + centroid) / (weight + 1.0)
                    norm = float(np.linalg.norm(mixed))
                    if norm > 1e-8:
                        mixed = mixed / norm
                    best_sp["embedding"] = mixed.tolist()
                    best_sp["embedding_provider"] = "pyannote"
                    best_sp["embedding_dim"] = int(centroid.size)
                    best_sp["appearances"] = appearances + max(turn_count, 1)
                    best_sp["total_audio_sec"] = float(
                        best_sp.get("total_audio_sec", 0.0)
                    ) + total_audio_sec
                    best_sp["updated_at"] = now
                    local_to_profile[local_speaker] = (sid, label, round(best_score, 3))
                    continue

                # 新規プロファイル作成
                auto_index = self._next_auto_index(speakers)
                sid = f"spk_{uuid.uuid4().hex[:8]}"
                label = f"話者{auto_index}"

                sample_rel: str | None = None
                if loaded is not None:
                    audio, sr = loaded
                    longest_turn = max(
                        (t for t in turns if t["speaker"] == local_speaker),
                        key=lambda t: t["end"] - t["start"],
                        default=None,
                    )
                    if longest_turn is not None:
                        s0 = max(0, int(longest_turn["start"] * sr))
                        s1 = min(len(audio), int(longest_turn["end"] * sr))
                        if s1 > s0:
                            sample_rel = self._write_sample_wav(sid, audio[s0:s1], sr)

                speakers.append({
                    "id": sid,
                    "label": label,
                    "auto_index": auto_index,
                    "embedding": centroid.tolist(),
                    "embedding_provider": "pyannote",
                    "embedding_dim": int(centroid.size),
                    "appearances": max(turn_count, 1),
                    "total_audio_sec": float(total_audio_sec),
                    "sample_wav": sample_rel,
                    "sample_session_id": session_id,
                    "created_at": now,
                    "updated_at": now,
                })
                used.add(sid)
                local_to_profile[local_speaker] = (sid, label, 1.0)

            # centroid が無いローカル speaker (embeddings 取得失敗時) は新規作成
            for local_speaker in {t["speaker"] for t in turns}:
                if local_speaker in local_to_profile:
                    continue
                auto_index = self._next_auto_index(speakers)
                sid = f"spk_{uuid.uuid4().hex[:8]}"
                label = f"話者{auto_index}"
                speakers.append({
                    "id": sid,
                    "label": label,
                    "auto_index": auto_index,
                    "embedding_provider": "pyannote",
                    "appearances": sum(
                        1 for _i, lbl in seg_to_local.items() if lbl == local_speaker
                    ),
                    "total_audio_sec": float(sum(
                        max(0.0, t["end"] - t["start"])
                        for t in turns if t["speaker"] == local_speaker
                    )),
                    "sample_session_id": session_id,
                    "created_at": now,
                    "updated_at": now,
                })
                used.add(sid)
                local_to_profile[local_speaker] = (sid, label, 0.0)

            raw["speakers"] = speakers
            self._save_raw(raw)

        # セグメントへラベル反映
        out: list[dict] = []
        prev_sid: str | None = None
        prev_label: str | None = None
        prev_conf: float | None = None
        for i, seg in enumerate(segments):
            cloned = dict(seg)
            cloned.pop("words", None)  # words は DB 保存サイズが大きくなるので落とす
            local = seg_to_local.get(i)
            if local is not None and local in local_to_profile:
                sid, label, conf = local_to_profile[local]
                cloned["speaker_id"] = sid
                cloned["speaker_label"] = label
                cloned["speaker_confidence"] = conf
                prev_sid, prev_label, prev_conf = sid, label, conf
            elif prev_sid is not None:
                cloned["speaker_id"] = prev_sid
                cloned["speaker_label"] = prev_label
                cloned["speaker_confidence"] = prev_conf
            out.append(cloned)

        _emit("assign", 1.0, "話者割り当て完了")
        return self.apply_latest_labels(out)

    def identify(
        self,
        audio: np.ndarray,
        sample_rate: int,
        *,
        session_id: str | None,
        start_sec: float,
        end_sec: float,
    ) -> dict[str, Any] | None:
        diarization_enabled = bool(
            config.get("whisper", "speaker_memory", "diarization_enabled", default=True)
        )
        if not diarization_enabled:
            return None

        enabled = bool(
            config.get("whisper", "speaker_memory", "enabled", default=True)
        )
        if not enabled:
            return None

        provider = str(
            config.get("whisper", "speaker_memory", "diarization_provider", default="legacy")
        ).lower()
        if provider == "pyannote":
            # pyannote 利用時はライブラベリングをスキップし、
            # 録音終了後の rediarize で一括ラベリングする
            return None

        emb = _extract_embedding(audio, sample_rate)
        if emb is None:
            return None

        threshold = float(
            config.get("whisper", "speaker_memory", "match_threshold", default=0.82)
        )
        threshold = max(0.0, min(0.99, threshold))
        now = _now_iso()

        with self._lock:
            raw = self._load_raw()
            speakers: list[dict[str, Any]] = raw.get("speakers", [])
            best: dict[str, Any] | None = None
            best_score = -1.0

            for sp in speakers:
                vec = sp.get("embedding")
                if not isinstance(vec, list) or not vec:
                    continue
                try:
                    old = np.asarray(vec, dtype=np.float32)
                except Exception:
                    continue
                if old.size != emb.size:
                    continue
                score = _cosine_similarity(emb, old)
                if score > best_score:
                    best_score = score
                    best = sp

            audio_sec = max(0.1, float(end_sec - start_sec))
            if best is not None and best_score >= threshold:
                sid = str(best.get("id"))
                label = str(best.get("label") or sid)
                appearances = int(best.get("appearances", 0))
                weight = float(min(100, max(1, appearances)))
                old = np.asarray(best.get("embedding"), dtype=np.float32)
                mixed = ((old * weight) + emb) / (weight + 1.0)
                mixed_norm = float(np.linalg.norm(mixed))
                if mixed_norm > 1e-8:
                    mixed = mixed / mixed_norm
                best["embedding"] = mixed.tolist()
                best["appearances"] = appearances + 1
                best["total_audio_sec"] = float(best.get("total_audio_sec", 0.0)) + audio_sec
                best["updated_at"] = now
                if not best.get("sample_wav"):
                    rel = self._write_sample_wav(sid, audio, sample_rate)
                    if rel:
                        best["sample_wav"] = rel
                        best["sample_session_id"] = session_id
                        best["sample_start_sec"] = round(start_sec, 2)
                        best["sample_end_sec"] = round(end_sec, 2)
                self._save_raw(raw)
                return {
                    "speaker_id": sid,
                    "speaker_label": label,
                    "speaker_confidence": round(best_score, 3),
                    "speaker_new": False,
                }

            auto_index = self._next_auto_index(speakers)
            sid = f"spk_{uuid.uuid4().hex[:8]}"
            label = f"話者{auto_index}"
            rel = self._write_sample_wav(sid, audio, sample_rate)
            speakers.append(
                {
                    "id": sid,
                    "label": label,
                    "auto_index": auto_index,
                    "embedding": emb.tolist(),
                    "appearances": 1,
                    "total_audio_sec": audio_sec,
                    "sample_wav": rel,
                    "sample_session_id": session_id,
                    "sample_start_sec": round(start_sec, 2),
                    "sample_end_sec": round(end_sec, 2),
                    "created_at": now,
                    "updated_at": now,
                }
            )
            raw["speakers"] = speakers
            self._save_raw(raw)
            return {
                "speaker_id": sid,
                "speaker_label": label,
                "speaker_confidence": 1.0,
                "speaker_new": True,
            }


speaker_memory = SpeakerMemory()
