"""Seam — 要約パイプライン精度テスト

Whisper 文字起こし結果を Qwen3 8B で議事録に変換し、品質を評価する。
テストケース:
  1. TTS 擬似会議 (正解がある) → 精度を定量評価
  2. 実音声 (10分日本語対談) → 生成品質を定性評価
"""
import time
from pathlib import Path

import httpx

OLLAMA_BASE = "http://localhost:11434"
MODEL = "qwen3:8b"

client = httpx.Client(base_url=OLLAMA_BASE, timeout=120)

# ─── Helper ───
def chat(messages: list[dict], temperature: float = 0.3) -> str:
    r = client.post("/api/chat", json={
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": 16384},
    })
    r.raise_for_status()
    return r.json()["message"]["content"]


print("=" * 70)
print("  Seam — 要約パイプライン精度テスト")
print("=" * 70)

# ─── Test 1: TTS 擬似会議 (正解あり) ───
print("\n" + "─" * 70)
print("[1] TTS 擬似会議 → 議事録生成")
print("─" * 70)

TTS_TRANSCRIPT = """[00:00] それでは定例ミーティングを始めましょう。今日のアジェンダはタグ機能の進捗確認と、来週のリリースに向けた準備です。まず田中さんからお願いします。
[00:12] はい、タグ機能のバックエンドAPIについて報告します。CRUDのエンドポイントは全て実装完了しました。テストも通っています。
[00:23] なるほど、バックエンドのAPIは完了しているんですね。フロントエンドの進捗はいかがですか？
[00:32] フロントエンドはタグの一覧表示と作成フォームまで完了しています。編集と削除の画面は今週中に完成予定です。
[00:42] 了解です。それでは次の議題に移ります。来週のリリースに向けて、パフォーマンステストの結果はどうでしたか？
[00:52] パフォーマンステストですが、データベースのクエリを最適化した結果、レスポンスタイムが平均で30%改善しました。ただし、大量のタグがある場合にページネーションが遅くなる問題が残っています。
[01:02] レスポンスタイムが改善されたのは良いですね。残りの課題を整理しましょう。
[01:08] 了解しました。ページネーションの問題はインデックスの追加で対応できると思います。明日中に修正してプルリクエストを出します。"""

MINUTES_PROMPT = """あなたは会議の議事録を作成するアシスタントです。
以下の会議の書き起こしから、議事録を作成してください。

## 出力フォーマット (厳守)

# 議事録: {会議の簡潔なタイトル}

**日時**: {不明の場合は省略}

---

## 概要
{会議全体の概要を1-3文で}

## 議論内容

### 1. {トピック名}
- {議論のポイント}

## 決定事項
- {決まったこと}

## アクションアイテム
- [ ] {内容} ({担当者})

## 次回への持ち越し
- {未解決の事項}

## ルール
- 情報を抜け漏れなく記録する
- 発言者を明記する
- 具体的な数字や固有名詞はそのまま残す
- 推測や解釈を加えず、発言内容に忠実に"""

print("議事録生成中...")
t0 = time.time()
tts_minutes = chat([
    {"role": "system", "content": MINUTES_PROMPT},
    {"role": "user", "content": f"## 書き起こし\n\n{TTS_TRANSCRIPT}"},
])
gen_time = time.time() - t0
print(f"生成時間: {gen_time:.1f}秒")

print("\n--- 生成された議事録 ---")
print(tts_minutes)

# 精度チェック: 必須情報が含まれているか
print("\n--- 精度チェック ---")
REQUIRED_ITEMS = [
    ("タグ機能", "メイントピック"),
    ("CRUD", "技術的詳細"),
    ("バックエンド", "レイヤー言及"),
    ("フロントエンド", "レイヤー言及"),
    ("一覧表示", "実装済み機能"),
    ("作成フォーム", "実装済み機能"),
    ("編集", "未完了機能"),
    ("削除", "未完了機能"),
    ("今週中", "期限"),
    ("パフォーマンステスト", "第2トピック"),
    ("30%", "具体的数字"),
    ("レスポンスタイム", "改善対象"),
    ("ページネーション", "残課題"),
    ("インデックス", "対策"),
    ("プルリクエスト", "アクション"),
    ("明日中", "期限"),
]

found = 0
for keyword, category in REQUIRED_ITEMS:
    if keyword in tts_minutes:
        print(f"  [OK] {keyword} ({category})")
        found += 1
    else:
        print(f"  [MISS] {keyword} ({category})")

accuracy = found / len(REQUIRED_ITEMS) * 100
print(f"\n情報網羅率: {found}/{len(REQUIRED_ITEMS)} ({accuracy:.0f}%)")

# 構造チェック
print("\n--- 構造チェック ---")
STRUCTURE = [
    ("# 議事録", "タイトル"),
    ("## 概要", "概要セクション"),
    ("## 議論内容", "議論内容セクション"),
    ("## 決定事項", "決定事項セクション"),
    ("## アクションアイテム", "アクションアイテム"),
    ("- [ ]", "チェックボックス"),
]
struct_found = 0
for pattern, label in STRUCTURE:
    if pattern in tts_minutes:
        print(f"  [OK] {label}")
        struct_found += 1
    else:
        print(f"  [MISS] {label}")

print(f"\n構造準拠率: {struct_found}/{len(STRUCTURE)} ({struct_found/len(STRUCTURE)*100:.0f}%)")


# ─── Test 2: 実音声 Whisper 結果 → 議事録 ───
print("\n" + "─" * 70)
print("[2] 実音声 (10分日本語対談) → 議事録生成")
print("─" * 70)

# Whisper で文字起こし
from faster_whisper import WhisperModel
import gc

AUDIO_FILE = Path("/tmp/seam-audio-test/meeting.wav")

if AUDIO_FILE.exists():
    print("Whisper 文字起こし中...")
    model = WhisperModel("medium", device="cpu", compute_type="int8")
    t0 = time.time()
    segments, info = model.transcribe(str(AUDIO_FILE), language="ja", vad_filter=True)
    segments = list(segments)
    whisper_time = time.time() - t0
    print(f"文字起こし: {whisper_time:.1f}秒 ({len(segments)} segments)")

    # Format as transcript
    transcript_lines = []
    for seg in segments:
        mm = int(seg.start // 60)
        ss = int(seg.start % 60)
        transcript_lines.append(f"[{mm:02d}:{ss:02d}] {seg.text}")
    real_transcript = "\n".join(transcript_lines)

    del model
    gc.collect()

    print(f"書き起こし文字数: {sum(len(s.text) for s in segments)}")

    # 長い書き起こしはチャンク分割して要約
    # 10分なので1チャンクで収まるはず
    print("\n議事録生成中...")
    t0 = time.time()
    real_minutes = chat([
        {"role": "system", "content": MINUTES_PROMPT},
        {"role": "user", "content": f"## 書き起こし\n\n{real_transcript}"},
    ])
    gen_time2 = time.time() - t0
    print(f"生成時間: {gen_time2:.1f}秒")

    print("\n--- 生成された議事録 ---")
    print(real_minutes)
else:
    print("  [SKIP] 実音声ファイルが見つかりません")
    real_minutes = ""
    gen_time2 = 0


# ─── Test 3: チャンク分割 + 段階的要約 ───
print("\n" + "─" * 70)
print("[3] チャンク分割 + 段階的要約テスト")
print("─" * 70)

if AUDIO_FILE.exists() and real_transcript:
    # 5分ごとに分割して部分要約 → 統合
    lines = real_transcript.split("\n")
    mid = len(lines) // 2
    chunk1 = "\n".join(lines[:mid])
    chunk2 = "\n".join(lines[mid:])

    print(f"チャンク1: {len(lines[:mid])} 行")
    print(f"チャンク2: {len(lines[mid:])} 行")

    print("\nチャンク1 部分要約中...")
    t0 = time.time()
    partial1 = chat([
        {"role": "system", "content": "以下の会議の書き起こし（一部）を要約してください。箇条書きで主要なポイントを網羅してください。発言内容を忠実に反映してください。"},
        {"role": "user", "content": chunk1},
    ])
    t1 = time.time() - t0
    print(f"  {t1:.1f}秒")

    print("チャンク2 部分要約中...")
    t0 = time.time()
    partial2 = chat([
        {"role": "system", "content": "以下の会議の書き起こし（一部）を要約してください。箇条書きで主要なポイントを網羅してください。発言内容を忠実に反映してください。"},
        {"role": "user", "content": chunk2},
    ])
    t2 = time.time() - t0
    print(f"  {t2:.1f}秒")

    print("統合議事録生成中...")
    t0 = time.time()
    merged_minutes = chat([
        {"role": "system", "content": MINUTES_PROMPT},
        {"role": "user", "content": f"""以下は会議の部分要約です。これらを統合して、完全な議事録を作成してください。

## 前半の要約
{partial1}

## 後半の要約
{partial2}"""},
    ])
    t3 = time.time() - t0
    print(f"  {t3:.1f}秒")

    print(f"\n段階的要約の合計時間: {t1+t2+t3:.1f}秒 (一括: {gen_time2:.1f}秒)")
    print("\n--- 段階的要約の議事録 ---")
    print(merged_minutes)
else:
    print("  [SKIP]")

# ─── Summary ───
print("\n" + "=" * 70)
print("  テストサマリ")
print("=" * 70)
print(f"  モデル: {MODEL}")
print(f"  TTS 議事録 情報網羅率: {accuracy:.0f}%")
print(f"  TTS 議事録 構造準拠率: {struct_found}/{len(STRUCTURE)}")
print(f"  TTS 議事録 生成時間: {gen_time:.1f}秒")
if gen_time2:
    print(f"  実音声 議事録 生成時間: {gen_time2:.1f}秒")
