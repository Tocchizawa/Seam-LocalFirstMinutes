"""Seam — 実音声パイプラインテスト (10分の日本語対談)"""
import gc
import time
from pathlib import Path

AUDIO_FILE = Path("/tmp/seam-audio-test/meeting.wav")
AUDIO_DURATION = 609.97  # 10:09.97

print("=" * 60)
print("  Seam — 実音声パイプラインテスト")
print("=" * 60)
print(f"\n音声: {AUDIO_FILE}")
print(f"サイズ: {AUDIO_FILE.stat().st_size / 1024 / 1024:.1f} MB")
print(f"長さ: {AUDIO_DURATION:.0f}秒 (~10分)")

# ─── 1. faster-whisper ───
print("\n" + "─" * 60)
print("[1] faster-whisper 文字起こし (medium, CPU, int8)")
print("─" * 60)

from faster_whisper import WhisperModel

print("モデルロード中...")
t0 = time.time()
model = WhisperModel("medium", device="cpu", compute_type="int8")
print(f"ロード: {time.time() - t0:.1f}秒")

print("文字起こし中...")
t0 = time.time()
segments_gen, info = model.transcribe(
    str(AUDIO_FILE),
    language="ja",
    vad_filter=True,
    vad_parameters=dict(min_silence_duration_ms=500),
)
segments = list(segments_gen)
transcribe_time = time.time() - t0

print(f"完了: {transcribe_time:.1f}秒 (リアルタイム倍率: {AUDIO_DURATION / transcribe_time:.1f}x)")
print(f"言語: {info.language} ({info.language_probability:.0%})")
print(f"セグメント数: {len(segments)}")

print("\n--- 全文書き起こし (先頭3分) ---")
for seg in segments:
    if seg.start > 180:
        print(f"  ... ({len([s for s in segments if s.start > 180])} segments after 3:00 omitted)")
        break
    ts = f"{int(seg.start // 60):02d}:{seg.start % 60:05.2f}"
    print(f"  [{ts}] {seg.text}")

# 末尾5セグメント
print("\n--- 末尾 5 セグメント ---")
for seg in segments[-5:]:
    ts = f"{int(seg.start // 60):02d}:{seg.start % 60:05.2f}"
    print(f"  [{ts}] {seg.text}")

# 文字数統計
full_text = "".join(seg.text for seg in segments)
print(f"\n総文字数: {len(full_text)}")
print(f"最終セグメント終了時刻: {segments[-1].end:.1f}秒 / {AUDIO_DURATION:.1f}秒")
coverage = segments[-1].end / AUDIO_DURATION * 100
print(f"カバー率: {coverage:.1f}%")

del model
gc.collect()

# ─── 2. ストリーミングシミュレーション ───
print("\n" + "─" * 60)
print("[2] ストリーミング文字起こしシミュレーション (5秒チャンク)")
print("─" * 60)

import numpy as np
import wave

model = WhisperModel("medium", device="cpu", compute_type="int8")

with wave.open(str(AUDIO_FILE), "rb") as wf:
    sr = wf.getframerate()
    n_frames = wf.getnframes()
    raw = wf.readframes(n_frames)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

chunk_sec = 5.0
overlap_sec = 1.0
chunk_samples = int(chunk_sec * sr)
overlap_samples = int(overlap_sec * sr)

n_chunks = 0
total_streaming_time = 0
streaming_segments = []
offset = 0.0
prev_tail = np.array([], dtype=np.float32)

print(f"サンプルレート: {sr} Hz, 総サンプル: {len(audio)}")
print(f"チャンクサイズ: {chunk_sec}秒, オーバーラップ: {overlap_sec}秒")
print()

pos = 0
while pos < len(audio):
    chunk = audio[pos:pos + chunk_samples]
    if len(chunk) < sr:  # 1秒未満は処理しない
        break

    has_overlap = len(prev_tail) > 0
    if has_overlap:
        chunk_with_overlap = np.concatenate([prev_tail, chunk])
    else:
        chunk_with_overlap = chunk

    overlap_end = overlap_sec if has_overlap else 0

    t0 = time.time()
    segs_gen, _ = model.transcribe(
        chunk_with_overlap,
        language="ja",
        vad_filter=True,
    )
    segs = list(segs_gen)
    elapsed = time.time() - t0
    total_streaming_time += elapsed

    for seg in segs:
        if seg.end <= overlap_end:
            continue
        new_start = max(seg.start, overlap_end) - overlap_end + offset
        new_end = seg.end - overlap_end + offset
        streaming_segments.append((new_start, new_end, seg.text))

    # 次回用オーバーラップ保存
    prev_tail = chunk[-overlap_samples:]
    offset += len(chunk) / sr
    pos += chunk_samples
    n_chunks += 1

    if n_chunks <= 3 or n_chunks % 10 == 0:
        print(f"  チャンク {n_chunks:3d}: {elapsed:.2f}秒 (音声 {chunk_sec}秒) {'OK' if elapsed < chunk_sec else 'SLOW'}")

print(f"\n合計: {n_chunks} チャンク, 処理時間: {total_streaming_time:.1f}秒")
print(f"平均チャンク処理時間: {total_streaming_time / n_chunks:.2f}秒 (< {chunk_sec}秒 = リアルタイム可能)")
print(f"ストリーミングセグメント数: {len(streaming_segments)}")

if streaming_segments:
    print("\n--- ストリーミング結果 (先頭3分) ---")
    for start, end, text in streaming_segments:
        if start > 180:
            remaining = len([s for s in streaming_segments if s[0] > 180])
            print(f"  ... ({remaining} segments after 3:00 omitted)")
            break
        ts = f"{int(start // 60):02d}:{start % 60:05.2f}"
        print(f"  [{ts}] {text}")

del model
gc.collect()

# ─── 3. サマリ ───
print("\n" + "=" * 60)
print("  テストサマリ")
print("=" * 60)
print(f"  音声長: {AUDIO_DURATION:.0f}秒 (~10分)")
print(f"  一括文字起こし: {transcribe_time:.1f}秒 ({AUDIO_DURATION / transcribe_time:.1f}x RT)")
print(f"  ストリーミング: 平均 {total_streaming_time / n_chunks:.2f}秒/チャンク {'(リアルタイム可能)' if total_streaming_time / n_chunks < chunk_sec else '(リアルタイム不可)'}")
print(f"  カバー率: {coverage:.1f}%")
print(f"  総文字数: {len(full_text)}")
