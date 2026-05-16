"""話者分離 + 文字起こしの精度を検証するスクリプト。

使い方:
    uv run python scripts/test_diarization.py <wav_path> [--seconds 60] [--provider legacy|pyannote|both]

出力:
    - チャンク化したセグメント
    - ハルシネーションフィルターで弾かれた件数
    - legacy / pyannote それぞれの話者分離結果サマリ
    - セグメントごとの time / speaker / text
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from collections import Counter
from math import gcd
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.transcribe.streaming import (
    SAMPLE_RATE, VADChunker, _resolve_repo, get_or_load_model,
    build_initial_prompt,
)
from src.transcribe.hallucination_filter import HallucinationFilter
from src.speakers import speaker_memory
from src.config import config


def load_wav_16k_mono(path: str, max_seconds: float | None = None) -> np.ndarray:
    """WAV → 16kHz mono float32。max_seconds で切り詰め可。"""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        nframes = wf.getnframes()
        # 必要分だけ読み込む (大きい WAV を避ける)
        if max_seconds is not None:
            nframes = min(nframes, int(sr * max_seconds))
        frames = wf.readframes(nframes)

    if sampwidth == 2:
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        audio = np.frombuffer(frames, dtype=np.float32)
    elif sampwidth == 1:
        audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth}")

    if nchannels > 1:
        audio = audio.reshape(-1, nchannels).mean(axis=1).astype(np.float32)

    if sr != SAMPLE_RATE:
        from scipy.signal import resample_poly
        g = gcd(SAMPLE_RATE, sr)
        audio = resample_poly(audio, SAMPLE_RATE // g, sr // g).astype(np.float32)
    return audio


def write_temp_wav(audio: np.ndarray, path: str) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def transcribe_audio(
    audio: np.ndarray,
    *,
    glossary: list[str],
    hall_filter: HallucinationFilter,
) -> tuple[list[dict], dict]:
    """retranscribe と同じロジックで文字起こし。"""
    import mlx_whisper
    model_name = config.get("whisper", "model", default="medium")
    repo = _resolve_repo(model_name)
    get_or_load_model(model_name)
    language = config.get("whisper", "language", default="ja")

    streaming_cfg = config.get("whisper", "streaming", default={}) or {}
    chunker = VADChunker(
        rms_threshold=streaming_cfg.get("rms_threshold", 0.005),
        silence_duration_ms=streaming_cfg.get("silence_duration_ms", 700),
        min_chunk_ms=streaming_cfg.get("min_chunk_ms", 1500),
        max_chunk_ms=streaming_cfg.get("max_chunk_ms", 15000),
    )
    BLOCK = SAMPLE_RATE // 10  # 100ms

    jobs: list[tuple[np.ndarray, float]] = []
    elapsed = 0.0
    for i in range(0, max(0, len(audio) - BLOCK + 1), BLOCK):
        block = audio[i:i + BLOCK]
        if len(block) == 0:
            break
        elapsed += len(block) / SAMPLE_RATE
        ch = chunker.feed(block)
        if ch is not None:
            dur = len(ch) / SAMPLE_RATE
            jobs.append((ch, max(0.0, elapsed - dur)))
    final = chunker.flush()
    if final is not None:
        dur = len(final) / SAMPLE_RATE
        jobs.append((final, max(0.0, elapsed - dur)))

    print(f"  [chunker] produced {len(jobs)} chunks (avg {elapsed:.1f}s audio)")

    segments: list[dict] = []
    filtered = 0
    recent_tail = ""
    for idx, (chunk, start_offset) in enumerate(jobs):
        initial_prompt = build_initial_prompt(glossary, recent_tail)
        kwargs: dict = {
            "path_or_hf_repo": repo,
            "language": language,
            "word_timestamps": True,  # ターン境界で分割するため有効化
            "verbose": False,
            "condition_on_previous_text": False,
            "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            "compression_ratio_threshold": 2.4,
            "logprob_threshold": -1.0,
            "no_speech_threshold": 0.6,
            "hallucination_silence_threshold": 2.0,
        }
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt
        try:
            result = mlx_whisper.transcribe(chunk.astype("float32"), **kwargs)
        except Exception as e:
            print(f"  [chunk {idx}] failed: {e}")
            continue
        chunk_texts = []
        for seg in result.get("segments", []):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            if hall_filter.is_hallucination(text):
                filtered += 1
                print(f"  [filter] dropped: {text[:80]}")
                continue
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
            segments.append({
                "start": round(start_offset + float(seg.get("start", 0)), 2),
                "end": round(start_offset + float(seg.get("end", 0)), 2),
                "text": text,
                "words": words,
            })
            chunk_texts.append(text)
        if chunk_texts:
            merged = (recent_tail + " " + " ".join(chunk_texts)).strip()
            from src.transcribe.streaming import PROMPT_RECENT_CHARS
            recent_tail = merged[-PROMPT_RECENT_CHARS:]

    return segments, {"chunks": len(jobs), "filtered": filtered}


def diarize_legacy(segments: list[dict], wav_path: str, session_id: str = "test") -> list[dict]:
    """legacy 話者分離。"""
    # 旧ロジックを直接呼ぶため、provider を一時的に legacy に固定
    original = config.get("whisper", "speaker_memory", "diarization_provider", default="legacy")
    config.set("whisper", "speaker_memory", "diarization_provider", value="legacy")
    try:
        return speaker_memory.rediarize_segments(
            segments, wav_path=wav_path, session_id=session_id,
        )
    finally:
        config.set("whisper", "speaker_memory", "diarization_provider", value=original)


def diarize_pyannote(segments: list[dict], wav_path: str, session_id: str = "test") -> list[dict]:
    """pyannote 話者分離。"""
    from src.speakers import pyannote_runner
    if not pyannote_runner.has_hf_token():
        raise RuntimeError("HF token がないので pyannote はスキップ")
    original = config.get("whisper", "speaker_memory", "diarization_provider", default="legacy")
    config.set("whisper", "speaker_memory", "diarization_provider", value="pyannote")
    try:
        return speaker_memory.rediarize_segments(
            segments, wav_path=wav_path, session_id=session_id,
        )
    finally:
        config.set("whisper", "speaker_memory", "diarization_provider", value=original)


def summarize(segments: list[dict], label: str) -> None:
    counts = Counter(s.get("speaker_label") or "?" for s in segments)
    speakers = list(counts.keys())
    total_dur = sum(max(0.0, s["end"] - s["start"]) for s in segments)
    print(f"\n=== {label} ===")
    print(f"  セグメント数: {len(segments)}, 総発話時間: {total_dur:.1f}s")
    print(f"  検出話者数: {len(speakers)}")
    for sp, cnt in counts.most_common():
        sp_dur = sum(
            max(0.0, s["end"] - s["start"])
            for s in segments if (s.get("speaker_label") or "?") == sp
        )
        print(f"    {sp}: {cnt} 発話 / {sp_dur:.1f}s")
    print("\n  --- 全セグメント ---")
    for s in segments:
        sp = s.get("speaker_label") or "?"
        text = (s.get("text") or "").replace("\n", " ")[:80]
        print(f"  [{s['start']:>6.1f}-{s['end']:>6.1f}] {sp:>8} | {text}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav_path")
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="解析する最大秒数 (default: 60)")
    ap.add_argument("--provider", choices=["legacy", "pyannote", "both"], default="both")
    ap.add_argument("--glossary", default="",
                    help="カンマ区切りの用語集 (任意)")
    ap.add_argument("--save-json", default=None,
                    help="結果を JSON で保存するパス")
    args = ap.parse_args()

    wav_path = str(Path(args.wav_path).expanduser())
    print(f"[load] {wav_path} (max {args.seconds}s)")
    audio = load_wav_16k_mono(wav_path, max_seconds=args.seconds)
    print(f"  loaded {len(audio) / SAMPLE_RATE:.1f}s of audio")

    # 一時 wav (chunker と diarize で同じ波形を共有)
    tmp_wav = "/tmp/diar_test_input.wav"
    write_temp_wav(audio, tmp_wav)

    glossary = [g.strip() for g in args.glossary.split(",") if g.strip()] if args.glossary else []
    if glossary:
        print(f"  glossary: {glossary}")

    hall_filter = HallucinationFilter.from_config(
        config.get("whisper", "hallucination_filter", default={}) or {}
    )

    print("\n[transcribe]")
    t0 = time.time()
    segments, stats = transcribe_audio(audio, glossary=glossary, hall_filter=hall_filter)
    t1 = time.time()
    print(f"  done in {t1 - t0:.1f}s -> {len(segments)} segments (filtered: {stats['filtered']})")

    results = {"transcribe_stats": stats, "segments_raw": [dict(s) for s in segments]}

    if args.provider in ("legacy", "both"):
        print("\n[diarize: legacy]")
        t0 = time.time()
        legacy_segs = diarize_legacy([dict(s) for s in segments], tmp_wav)
        t1 = time.time()
        print(f"  done in {t1 - t0:.1f}s")
        summarize(legacy_segs, "LEGACY")
        results["legacy"] = legacy_segs

    if args.provider in ("pyannote", "both"):
        print("\n[diarize: pyannote]")
        try:
            t0 = time.time()
            pyannote_segs = diarize_pyannote([dict(s) for s in segments], tmp_wav)
            t1 = time.time()
            print(f"  done in {t1 - t0:.1f}s")
            summarize(pyannote_segs, "PYANNOTE")
            results["pyannote"] = pyannote_segs
        except Exception as e:
            print(f"  pyannote: skipped ({e})")
            results["pyannote_error"] = str(e)

    if args.save_json:
        with open(args.save_json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n[saved] {args.save_json}")


if __name__ == "__main__":
    main()
