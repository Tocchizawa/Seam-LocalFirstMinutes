# Seam - データモデル設計書

## 1. 設定ファイル (YAML)

### 1.1 グローバル設定 (`~/.seam/config.yaml`)

```yaml
schema_version: 1                 # 設定ファイルバージョン（マイグレーション用）

# LLM設定
ollama:
  base_url: "http://localhost:11434"
  context_model: "qwen3:8b"
  auto_start: true
  auto_install: true

minutes_ai:
  provider: "ollama"
  auto_generate: true
  ollama:
    model: "qwen3:8b"
    num_ctx: 8192

# Whisper設定
whisper:
  model: "medium"               # tiny / base / small / medium / large-v3
  language: "ja"
  device: "auto"                # auto / cpu / mps
  streaming:
    silence_duration_ms: 500
    min_chunk_ms: 1000
    max_chunk_ms: 12000

# 録音設定
recording:
  mic_device: null              # null = システム既定
  system_capture: "auto"        # auto / coreaudio_tap
  sample_rate: 16000
  channels: 1
  system_capture_watchdog:
    enabled: true               # 内部音声が途中で途切れた録音を正常完了扱いしない
    min_duration_sec: 60
    min_coverage_ratio: 0.85
    max_missing_sec: 20
  audio_leveling:
    enabled: true               # 録音中/停止後の自動ゲイン補正
    realtime_enabled: true      # Whisper投入前のリアルタイム補正
    final_normalize: true       # combined.flac 生成時の動的正規化
    target_rms: 0.08
    noise_floor: 0.003
    max_gain: 12.0
    peak_limit: 0.95
    frame_ms: 100
    gauss_size: 3

# エージェント設定
agent:
  max_steps: 15
  max_file_lines: 500
  context_timeout_sec: 60
  max_context_tokens: 28000     # 32K のうちシステムプロンプト用に 4K 確保

# サーバー設定
server:
  host: "127.0.0.1"
  port: 18900

# ログ設定
logging:
  dir: "~/.seam/logs/"      # ログ出力先
  level: "INFO"                 # DEBUG / INFO / WARNING / ERROR
  max_size_mb: 50               # ローテーション: 1ファイルの上限
  backup_count: 3               # 保持するバックアップ数

# セットアップ状態
setup:
  completed: true
```

**全フィールドにデフォルト値を持たせる**: 新バージョンでフィールドが追加された場合、欠落しているキーはコード側のデフォルト値で補完。`schema_version` でマイグレーション判定。

### 1.2 プロジェクト一覧 (`~/.seam/projects.yaml`)

```yaml
schema_version: 1

projects:
  - id: "egaite"
    name: "えがいて"
    repo_path: "/path/to/workspace/egaite"  # nullable
    doc_dirs:                    # 空リストでもOK
      - "/path/to/workspace/egaite/docs"
    output_dir: "/path/to/exports/egaite"  # 自動作成
    members:
      - name: "とちざわ"
        role: "リード"
    glossary:
      - "Supabase: BaaS。データベースと認証を提供"
      - "OG画像: SNSでシェアした時のプレビュー画像"
    created_at: "2026-04-03T14:30:00"
    updated_at: "2026-04-03T14:30:00"
```

---

## 2. セッションデータ（パイプライン作業用・一時的）

1回の会議 = 1セッション。**パイプライン完了後にディレクトリごと全削除。**

### 2.1 state.json

```json
{
  "session_id": "20260403_143000_abc123",
  "project_id": "egaite",
  "current_stage": "context_investigating",
  "started_at": "2026-04-03T14:30:00",
  "stopped_at": "2026-04-03T15:15:00",
  "duration_sec": 2700,
  "devices": {
    "mic": { "name": "MacBook Pro Microphone", "device_id": 1 },
    "system": { "method": "coreaudio_tap" }
  },
  "stages": {
    "recording":              { "status": "completed", "completed_at": "2026-04-03T15:15:00" },
    "flushing_whisper":       { "status": "completed", "completed_at": "2026-04-03T15:15:05" },
    "converting_wav":         { "status": "completed", "completed_at": "2026-04-03T15:15:30" },
    "mixing":                 { "status": "completed", "completed_at": "2026-04-03T15:16:00" },
    "context_investigating":  { "status": "in_progress" },
    "generating_minutes":     { "status": "pending" },
    "saving":                 { "status": "pending" },
    "cleaning_up":            { "status": "pending" }
  }
}
```

### 2.2 セッションファイル構成（全て一時ファイル）

```
~/.seam/sessions/{session_id}/
├── state.json
├── mic.raw / mic.wav
├── system.raw / system.wav
├── combined.flac
├── streaming_transcript.json
├── context.json
└── minutes.md
```

→ COMPLETED 後に**ディレクトリごと削除**。正本は SQLite + output_dir。

---

## 3. SQLite スキーマ（正本）

単一の `~/.seam/minutes.db` に全プロジェクトの議事録を保存する。

**単一 DB の理由**: 要件 ST-08「議事録のプロジェクト再割り当て」でプロジェクト間の議事録移動が必要。プロジェクトごとに DB を分離すると、異なる DB 間の INSERT + DELETE でトランザクションの原子性が保証できない。

```sql
CREATE TABLE minutes (
    rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL UNIQUE,    -- UUID (アプリケーション識別子)
    session_id      TEXT NOT NULL UNIQUE,
    project_id      TEXT NOT NULL,
    title           TEXT NOT NULL,
    date            TEXT NOT NULL,           -- YYYY-MM-DD
    started_at      TEXT NOT NULL,
    duration_sec    INTEGER NOT NULL,
    transcript      TEXT NOT NULL,               -- JSON array
    transcript_text TEXT NOT NULL DEFAULT '',     -- FTS 用: transcript の text フィールドのみ結合した平文
    summary         TEXT NOT NULL,               -- Markdown
    context_snapshot TEXT,                        -- JSON (nullable)
    whisper_model   TEXT,
    llm_model       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX idx_minutes_project_id ON minutes(project_id);
CREATE INDEX idx_minutes_date ON minutes(date);

-- 全文検索 (trigram tokenizer: 日本語部分一致対応、追加ライブラリ不要)
-- transcript_text: transcript JSON から text フィールドのみを結合した平文。
-- JSON 構文 ([, {, ", :) がトライグラムインデックスを汚染するのを防止。
-- INTEGER PRIMARY KEY (rowid) を content_rowid に使用するため、
-- TEXT PRIMARY KEY + 暗黙 rowid の不確実性を回避。
CREATE VIRTUAL TABLE minutes_fts USING fts5(
    title,
    summary,
    transcript_text,
    content='minutes',
    content_rowid='rowid',
    tokenize='trigram'
);

-- FTS 自動同期トリガー
CREATE TRIGGER minutes_ai AFTER INSERT ON minutes BEGIN
    INSERT INTO minutes_fts(rowid, title, summary, transcript_text)
    VALUES (new.rowid, new.title, new.summary, new.transcript_text);
END;

CREATE TRIGGER minutes_ad AFTER DELETE ON minutes BEGIN
    INSERT INTO minutes_fts(minutes_fts, rowid, title, summary, transcript_text)
    VALUES ('delete', old.rowid, old.title, old.summary, old.transcript_text);
END;

CREATE TRIGGER minutes_au AFTER UPDATE ON minutes BEGIN
    INSERT INTO minutes_fts(minutes_fts, rowid, title, summary, transcript_text)
    VALUES ('delete', old.rowid, old.title, old.summary, old.transcript_text);
    INSERT INTO minutes_fts(rowid, title, summary, transcript_text)
    VALUES (new.rowid, new.title, new.summary, new.transcript_text);
END;
```

### 3.0 transcript_text の生成

アプリケーション層で INSERT 前に transcript JSON から平文を生成する:

```python
def build_transcript_text(transcript: list[dict]) -> str:
    """transcript JSON array から FTS 用の平文テキストを生成"""
    return "\n".join(seg["text"] for seg in transcript if seg.get("text"))
```

### 3.1 transcript (JSON)

```json
[
  { "start": 0.0, "end": 3.52, "text": "じゃあ始めましょう" },
  { "start": 3.8, "end": 7.21, "text": "よろしくお願いします" }
]
```

### 3.2 action_items

議事録本文 (summary) 内に Markdown チェックリストとして含む。別テーブル/JSONには分離しない。

---

## 4. Markdown 出力（正本）

プロジェクト設定の `output_dir` に保存（存在しなければ自動作成）。

**注意**: output_dir の Markdown は SQLite から常に再生成可能。ユーザーが手動編集した内容は再生成時に失われる（正本は SQLite）。

### 4.1 議事録

ファイル名: `{YYYY-MM-DD}_{HHmm}_{session_id_short}.md`
（session_id_short = session_id の末尾6文字。衝突回避のため日本語タイトルではなく ID を使用）

```markdown
# 議事録: {プロジェクト名} - {YYYY/MM/DD}

**日時**: {YYYY年M月D日} {HH:MM} - {HH:MM} ({N}分)
**プロジェクト**: {プロジェクト名}

---

## 概要

{会議全体の概要を1-3文で}

## 議論内容

### 1. {トピック名}

- {議論のポイント}

### 2. {トピック名}

- ...

## 決定事項

- {決まったこと}

## アクションアイテム

- [ ] {内容} ({担当者} / {期限})

## 次回への持ち越し

- {未解決の事項}
```

### 4.2 全文書き起こし

ファイル名: `{YYYY-MM-DD}_{HHmm}_{session_id_short}_transcript.md`

```markdown
# 全文書き起こし: {プロジェクト名} - {YYYY/MM/DD}

**日時**: {YYYY年M月D日} {HH:MM} - {HH:MM}

---

[00:00:00] じゃあ始めましょう

[00:00:03] よろしくお願いします

...
```

---

## 5. データライフサイクル

```
録音開始
  │
  ├── RAW PCM 書き込み (mic.raw, system.raw)   [一時]
  ├── リアルタイムミックス → Whisper → GUI       [一時]
  │
録音停止
  │
  ├── Whisper バッファ flush                     [保存用 transcript の確定]
  ├── RAW → WAV 変換                            [一時]
  ├── ミックス → combined.flac                   [再生・手動再文字起こし用]
  │
コンテキスト調査
  │
  ├── Context Agent → context.json               [一時]
  │
議事録生成
  │
  ├── Minutes Agent → minutes.md                  [一時]
  │
保存
  │
  ├── SQLite INSERT (minutes.db)                  [正本]
  ├── Markdown 出力 → output_dir/                 [正本]
  ├── 全文書き起こし Markdown → output_dir/        [正本]
  │
クリーンアップ
  │
  └── セッションディレクトリ全削除                   [一時データ全消去]
```
