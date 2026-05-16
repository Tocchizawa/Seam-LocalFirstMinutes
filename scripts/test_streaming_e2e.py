"""StreamingTranscriber を MP3 で end-to-end テスト。

実行例:
    uv run python scripts/test_streaming_e2e.py --mp3 ./sample.mp3
    uv run python scripts/test_streaming_e2e.py --large    # large-v3 で
    uv run python scripts/test_streaming_e2e.py --duration 60
    SEAM_TEST_MP3=./sample.mp3 uv run python scripts/test_streaming_e2e.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.transcribe.streaming import StreamingTranscriber, SAMPLE_RATE


DEFAULT_MP3_PATH = os.environ.get("SEAM_TEST_MP3", "").strip()


def decode_mp3(path: str, target_rate: int = 16000) -> np.ndarray:
    """MP3 → 16kHz mono float32 numpy array."""
    proc = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-i", path,
            "-f", "f32le", "-acodec", "pcm_f32le",
            "-ar", str(target_rate), "-ac", "1", "-",
        ],
        capture_output=True, check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--large", action="store_true", help="large-v3 を使う(遅い)")
    ap.add_argument("--duration", type=int, default=90, help="テスト秒数")
    ap.add_argument("--no-realtime", action="store_true", help="リアルタイム pacing 無し")
    ap.add_argument("--threshold", type=float, default=0.005)
    ap.add_argument(
        "--mp3",
        default=DEFAULT_MP3_PATH,
        help="入力 MP3 パス（未指定時は SEAM_TEST_MP3 を参照）",
    )
    args = ap.parse_args()
    mp3_path = Path(args.mp3).expanduser() if args.mp3 else None
    if mp3_path is None:
        print("error: --mp3 または SEAM_TEST_MP3 を指定してください")
        sys.exit(2)
    if not mp3_path.is_file():
        print(f"error: MP3 が見つかりません: {mp3_path}")
        sys.exit(2)

    print("=" * 70)
    print(f"  Streaming E2E Test — {mp3_path.name}")
    print("=" * 70)

    print("\n[1] MP3 デコード中...")
    t0 = time.time()
    audio = decode_mp3(str(mp3_path))
    print(f"    全長: {len(audio) / SAMPLE_RATE:.1f}秒 / デコード時間 {time.time() - t0:.1f}秒")

    # テスト範囲を切り出し
    test_samples = int(args.duration * SAMPLE_RATE)
    audio = audio[:test_samples]
    print(f"    テスト対象: 先頭 {len(audio) / SAMPLE_RATE:.1f}秒")

    # 全体 RMS 統計
    block = SAMPLE_RATE // 10
    rmss = []
    for i in range(0, len(audio) - block, block):
        rmss.append(float(np.sqrt(np.mean(audio[i:i+block]**2))))
    rmss_arr = np.array(rmss)
    above = (rmss_arr >= args.threshold).sum()
    print(f"\n[2] RMS 分布(100ms ブロック × {len(rmss)}個)")
    print(f"    min={rmss_arr.min():.5f} / max={rmss_arr.max():.5f} / mean={rmss_arr.mean():.5f}")
    print(f"    閾値 {args.threshold} 以上のブロック: {above}/{len(rmss)} ({above/len(rmss)*100:.1f}%)")

    # Streaming 起動
    model = "large-v3" if args.large else "medium"
    print(f"\n[3] StreamingTranscriber 起動 (model={model})")

    segments: list[dict] = []
    first_segment_at: float | None = None

    async def on_segment(seg: dict) -> None:
        nonlocal first_segment_at
        if first_segment_at is None:
            first_segment_at = time.time()
        segments.append(seg)
        print(f"      ← [{seg['start']:6.1f}s-{seg['end']:6.1f}s] {seg['text']}")

    # asyncio loop を別スレッドで動かす(本番と同じ環境を再現)
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    transcriber = StreamingTranscriber(
        model_name=model,
        on_segment=on_segment,
        chunker_kwargs={"rms_threshold": args.threshold,
                        "silence_duration_ms": 700,
                        "min_chunk_ms": 1500,
                        "max_chunk_ms": 15000},
    )
    transcriber.start(loop)

    # 音声を 100ms ブロックで feed
    BLOCK_MS = 100
    block_size = int(SAMPLE_RATE * BLOCK_MS / 1000)
    n_blocks = len(audio) // block_size

    print(f"\n[4] 音声を {n_blocks}ブロック (各 {BLOCK_MS}ms) で feed"
          f"{' (リアルタイム pacing)' if not args.no_realtime else ' (最高速)'}")

    record_start = time.time()
    last_status = record_start
    for i in range(n_blocks):
        b = audio[i*block_size:(i+1)*block_size]
        transcriber.feed(b)

        if not args.no_realtime:
            target = (i + 1) * BLOCK_MS / 1000
            elapsed = time.time() - record_start
            if target > elapsed:
                time.sleep(target - elapsed)

        # 5秒ごとに状態を print
        now = time.time()
        if now - last_status >= 5:
            sec = (i + 1) * BLOCK_MS / 1000
            print(f"   [t={sec:5.1f}s] model_loaded={transcriber.is_model_loaded} "
                  f"queue={transcriber.queue_size} segments={transcriber.total_segments} "
                  f"last_rms={transcriber.last_rms:.4f} peak={transcriber.peak_rms:.4f}")
            last_status = now

    record_end = time.time()
    record_duration = record_end - record_start
    print("\n[5] feed 終了")
    print(f"    feed 時間: {record_duration:.1f}秒 (音声 {len(audio)/SAMPLE_RATE:.1f}秒)")
    print(f"    flush 前の状態: queue={transcriber.queue_size} segments={transcriber.total_segments}")
    if first_segment_at is not None:
        print(f"    最初の segment 到達: 録音開始から {first_segment_at - record_start:.1f}秒")
    else:
        print("    最初の segment: ⚠ まだ来てない")

    # Flush
    print("\n[6] flush(残処理ドレイン)")
    flush_start = time.time()
    final = transcriber.flush()
    flush_duration = time.time() - flush_start
    print(f"    flush 時間: {flush_duration:.1f}秒")
    print(f"    最終 segment 数: {len(final)}")

    # サマリ
    audio_dur = len(audio) / SAMPLE_RATE
    total_wall = (record_end - record_start) + flush_duration
    print("\n[7] サマリ")
    print(f"    音声長:           {audio_dur:.1f}秒")
    print(f"    feed 時間:        {record_duration:.1f}秒")
    print(f"    flush 残処理:     {flush_duration:.1f}秒")
    print(f"    全体 wall clock:  {total_wall:.1f}秒")
    if not args.no_realtime:
        if audio_dur > 0:
            saved_pct = (audio_dur - flush_duration) / audio_dur * 100
            print("    バッチ比較:")
            print(f"      バッチ想定:   {audio_dur:.1f}秒録音 + バッチ処理"
                  f" ≈ {audio_dur:.1f} + 大体 {audio_dur*(2 if args.large else 0.7):.0f}秒")
            print(f"      ストリーミング: {audio_dur:.1f}秒 + {flush_duration:.1f}秒 残処理")
            print(f"      停止後の待ち削減効果: ~{saved_pct:.0f}%")

    transcriber.cleanup()
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=2)

    print()
    print("=" * 70)
    if len(final) > 0:
        print(f"  ✅ PASS — segment が {len(final)} 個生成された")
    else:
        print("  ❌ FAIL — segment が0個。VAD 閾値 / 音声入力を確認")
    print("=" * 70)


if __name__ == "__main__":
    main()
