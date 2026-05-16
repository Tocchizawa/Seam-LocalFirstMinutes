"""Seam の WebSocket を傍受してメッセージを print する診断ツール。

使い方:
    1. Seam.app を起動
    2. このスクリプトを別ターミナルで実行: uv run python scripts/ws_debug.py
    3. アプリで録音開始 → どのメッセージが届くか確認
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter

import websockets

URL = "ws://127.0.0.1:18900/ws"


async def main() -> None:
    print(f"Connecting to {URL} ...")
    try:
        async with websockets.connect(URL) as ws:
            print("✅ Connected.\n録音開始するとここにメッセージが出ます。Ctrl-C で終了。\n")
            counts: Counter = Counter()
            last_summary = time.time()
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"⚠ non-JSON: {raw[:100]}")
                    continue
                t = msg.get("type", "?")
                counts[t] += 1
                data = msg.get("data") or {}

                if t == "transcript_chunk":
                    print(f"  🟢 [{data.get('start',0):6.1f}s-{data.get('end',0):6.1f}s] "
                          f"{data.get('text','')}")
                elif t == "streaming_status":
                    print(f"  🔵 model_loaded={data.get('model_loaded')} "
                          f"queue={data.get('queue_size')} "
                          f"segments={data.get('total_segments')} "
                          f"rms={data.get('last_rms')}/{data.get('rms_threshold')}")
                elif t == "pipeline_progress":
                    print(f"  ⚙  {data.get('state')}: {data.get('message','')}")
                elif t == "pipeline_done":
                    print(f"  ✅ pipeline_done: {data.get('count')}件")
                elif t == "pipeline_error":
                    print(f"  ❌ pipeline_error: {data}")
                elif t == "recording_stopped":
                    print(f"  🛑 recording_stopped: {data.get('duration_sec')}秒")
                # audio_level / recording_status はうるさいので集計のみ

                # 5秒ごとにサマリ
                if time.time() - last_summary >= 5:
                    summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                    print(f"  ── 5秒サマリ: {summary}")
                    last_summary = time.time()
    except (ConnectionRefusedError, OSError) as e:
        print(f"❌ Connection failed: {e}")
        print("   Seam.app が起動していて、port 18900 で listen していますか?")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
