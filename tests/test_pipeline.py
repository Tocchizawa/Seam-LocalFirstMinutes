"""Seam — 音声パイプライン精度テスト

faster-whisper (文字起こし) のエンドツーエンドテスト。
"""
import time
from pathlib import Path

AUDIO_FILE = Path("/tmp/seam-audio-test/tts_meeting.wav")

# === Ground Truth (TTS で生成した内容) ===
GROUND_TRUTH = [
    "それでは定例ミーティングを始めましょう。今日のアジェンダはタグ機能の進捗確認と、来週のリリースに向けた準備です。まず田中さんからお願いします。",
    "はい、タグ機能のバックエンドAPIについて報告します。CRUDのエンドポイントは全て実装完了しました。テストも通っています。",
    "なるほど、バックエンドのAPIは完了しているんですね。フロントエンドの進捗はいかがですか？",
    "フロントエンドはタグの一覧表示と作成フォームまで完了しています。編集と削除の画面は今週中に完成予定です。",
    "了解です。それでは次の議題に移ります。来週のリリースに向けて、パフォーマンステストの結果はどうでしたか？",
    "パフォーマンステストですが、データベースのクエリを最適化した結果、レスポンスタイムが平均で30パーセント改善しました。ただし、大量のタグがある場合にページネーションが遅くなる問題が残っています。",
    "レスポンスタイムが改善されたのは良いですね。残りの課題を整理しましょう。",
    "了解しました。ページネーションの問題はインデックスの追加で対応できると思います。明日中に修正してプルリクエストを出します。",
]

print("=" * 60)
print("  Seam — 音声パイプライン精度テスト")
print("=" * 60)
print(f"\n音声ファイル: {AUDIO_FILE}")
print(f"ファイルサイズ: {AUDIO_FILE.stat().st_size / 1024:.1f} KB")

# ─── 1. faster-whisper 文字起こし ───
print("\n" + "─" * 60)
print("[1] faster-whisper 文字起こし")
print("─" * 60)

from faster_whisper import WhisperModel

print("モデルロード中 (medium)...")
t0 = time.time()
model = WhisperModel("medium", device="cpu", compute_type="int8")
load_time = time.time() - t0
print(f"モデルロード: {load_time:.1f}秒")

print("文字起こし実行中...")
t0 = time.time()
segments_gen, info = model.transcribe(
    str(AUDIO_FILE),
    language="ja",
    vad_filter=True,
)
segments = list(segments_gen)
transcribe_time = time.time() - t0

print(f"文字起こし完了: {transcribe_time:.1f}秒")
print(f"検出言語: {info.language} (確信度: {info.language_probability:.2%})")
print(f"セグメント数: {len(segments)}")
print()

print("--- Whisper 出力 ---")
full_text = ""
for seg in segments:
    print(f"  [{seg.start:6.1f}s - {seg.end:6.1f}s] {seg.text}")
    full_text += seg.text

# 精度チェック: Ground Truth のキーワードがどれだけ含まれているか
keywords = [
    "定例ミーティング", "アジェンダ", "タグ機能", "進捗",
    "バックエンド", "API", "CRUD", "エンドポイント",
    "フロントエンド", "一覧表示", "作成フォーム",
    "パフォーマンステスト", "データベース", "クエリ", "最適化",
    "レスポンスタイム", "30", "ページネーション",
    "インデックス", "プルリクエスト",
]

print("\n--- キーワード検出率 ---")
found = 0
for kw in keywords:
    if kw in full_text:
        found += 1
        status = "OK"
    else:
        status = "MISS"
    print(f"  [{status}] {kw}")

accuracy = found / len(keywords) * 100
print(f"\nキーワード検出率: {found}/{len(keywords)} ({accuracy:.0f}%)")

# Whisper モデルアンロード
del model
import gc
gc.collect()

# ─── 2. サマリ ───
print("\n" + "=" * 60)
print("  テストサマリ")
print("=" * 60)
print("  Whisper モデル: medium (CPU, int8)")
print(f"  文字起こし時間: {transcribe_time:.1f}秒 (音声: 69秒)")
print(f"  リアルタイム倍率: {69 / transcribe_time:.1f}x")
print(f"  キーワード検出率: {accuracy:.0f}%")
print(f"  セグメント数: {len(segments)}")
