# Seam - アーキテクチャ設計書

## 1. 全体構成

```
┌──────────────────────────────────────────────────────────────┐
│                     GUI (Tauri + React)                       │
│  プロジェクト管理 / 録音操作 / リアルタイム表示 / 議事録ビューア │
│                                                              │
│  Rust側: native audio sidecar / Python Backend 管理             │
└────────────────────────┬─────────────────────────────────────┘
                         │ HTTP / WebSocket
┌────────────────────────▼─────────────────────────────────────┐
│                  Python Backend (FastAPI)                      │
│                                                               │
│  Phase 1: 録音中                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                 │
│  │ mic      │──→│ Realtime │──→│ Streaming│──→ WS → GUI     │
│  │ capture  │   │ Mixer    │   │ Whisper  │                   │
│  └──────────┘   └──────────┘   └──────────┘                 │
│  ┌──────────┐        ↑                                       │
│  │ system   │────────┘  ※両トラックを即座にミックス            │
│  │ capture  │                                                 │
│  └──────────┘                                                │
│  (各トラックは RAW PCM で個別保存)                              │
│                                                               │
│  Phase 2: 録音停止後 (順次実行・都度アンロード)                  │
│  ┌─────────┐  ┌─────────┐  ┌──────────┐                       │
│  │ Flush   │→│ Mix     │→│ Context  │                       │
│  │ Whisper │  │ tracks  │  │ Agent    │                       │
│  │→unload  │  │→WAV     │  │ (ReAct)  │                       │
│  └─────────┘  └─────────┘  └────┬─────┘                       │
│                                  │                              │
│                            ┌──────▼─────┐                      │
│                            │ Minutes    │                      │
│                            │ Agent      │                      │
│                            └──────┬─────┘                      │
│                                  │                              │
│                            ┌──────▼─────┐                      │
│                            │ Save+Clean │                      │
│                            │ DB+MD→削除  │                      │
│                            └────────────┘                      │
└───────────────────────────────────────────────────────────────┘
```

### メモリ管理（順次ロード・アンロード）

```
Phase 1: 録音中
  Whisper medium:   ~3.0GB
  録音+ミキシング:    ~0.5GB
  Python+Tauri:     ~0.8GB
  ──────────────────────
  アプリ合計:       ~4.3GB (+macOS+Zoom ~5GB = ~9.3GB / 16GB)

Phase 2: LLM (コンテキスト調査 + 議事録生成)
  Qwen3 8B:         ~5.0GB → 完了後アンロード
  Python+Tauri:     ~0.8GB
  ──────────────────────
  アプリ合計:       ~5.8GB
```

---

## 2. モジュール構成

### Layer 1: GUI (Tauri + React)

```
gui/
├── src-tauri/
│   └── src/
│       ├── main.rs               # ウィンドウ管理
│       └── sidecar.rs            # Python Backend + native sidecar 起動・管理
├── sidecar/
│   └── audio-capture/            # Objective-C sidecar (ScreenCaptureKit + Core Audio mic)
│       └── Sources/
│           └── main.m            # システム音声/マイク → raw PCM 書き出し
│
└── src/
    ├── App.tsx
    ├── pages/
    │   ├── Home.tsx              # メイン画面
    │   ├── Recording.tsx         # 録音中画面（リアルタイム文字起こし）
    │   ├── Processing.tsx        # 後処理進捗画面
    │   ├── MinutesView.tsx       # 議事録ビューア（要約 + 全文タブ）
    │   ├── Settings.tsx          # 設定画面
    │   └── Setup.tsx             # 初回セットアップウィザード
    ├── components/
    │   ├── ProjectList.tsx
    │   ├── RecordDialog.tsx
    │   ├── AudioMeter.tsx
    │   ├── LiveTranscript.tsx
    │   ├── PipelineProgress.tsx
    │   └── MarkdownViewer.tsx
    └── lib/
        └── api.ts
```

### Layer 2: Python Backend (FastAPI)

```
src/
├── main.py                  # FastAPI + Uvicorn
├── config.py                # 設定・パス管理
│
├── api/
│   ├── projects.py          # /api/projects
│   ├── recording.py         # /api/recording
│   ├── minutes.py           # /api/minutes
│   ├── devices.py           # /api/devices
│   ├── settings.py          # /api/settings
│   └── ws.py                # WebSocket
│
├── project/
│   ├── manager.py           # CRUD
│   └── models.py            # Project, Member
│
├── context/
│   ├── agent.py             # Context Agent (ReAct + スライディングウィンドウ)
│   ├── tools.py             # ツール群 (read_file, list_dir, search, git_*, run_python)
│   ├── prompt.py            # システムプロンプト
│   ├── window.py            # コンテキストウィンドウ管理 (古い結果をトランケート)
│   └── file_readers.py      # 各種ファイル形式読み取り
│
├── audio/
│   ├── recorder.py          # 2トラック同時録音オーケストレーション
│   ├── mic_capture.py       # sounddevice でマイク録音
│   ├── system_capture.py    # ScreenCaptureKit sidecar からシステム音声受信
│   ├── mixer.py             # リアルタイムミキシング + 自動ゲイン補正
│   ├── devices.py           # デバイス一覧
│   ├── raw_writer.py        # RAW PCM 書き込み
│   ├── converter.py         # RAW→WAV 変換 (ffmpeg subprocess)
│   └── sync.py              # モノトニッククロック同期
│
├── transcribe/
│   ├── streaming.py         # ストリーミングWhisper (run_in_executor)
│   └── model_manager.py     # Whisperモデルのロード/アンロード
│
├── summarize/
│   ├── agent.py             # Minutes Agent
│   ├── chunker.py           # 話題ベースチャンク分割
│   └── prompt.py            # プロンプトテンプレート
│
├── pipeline/
│   ├── orchestrator.py      # パイプライン管理 (状態遷移・再開)
│   └── state.py             # PipelineStage enum + state.json 操作
│
├── storage/
│   ├── db.py                # SQLite (CRUD + FTS5)
│   ├── models.py            # スキーマ定義
│   └── export.py            # Markdown出力 + 再出力
│
├── logging_config.py        # ログ設定 (RotatingFileHandler, ~/.seam/logs/)
│
└── llm/
    ├── client.py            # Ollama API
    ├── tool_call.py         # Tool calling パーサー
    └── startup.py           # Ollama プロセス起動・停止 (Phase 2 の LLM 使用直前に起動、完了後に停止)
```

---

## 3. コンポーネント詳細

### 3.1 配布・起動

```
GijirokuN.app/Contents/
├── MacOS/Seam              # Tauri (メインプロセス)
├── Resources/
│   ├── backend/                 # PyInstaller --onedir 出力
│   │   ├── seam-backend     # エントリーポイント
│   │   └── _internal/           # Python + 依存 (~3GB)
│   ├── audio-capture            # native audio sidecar
│   ├── ollama/ollama            # Ollama バイナリ (~200MB)
│   └── ffmpeg/ffmpeg            # ffmpeg バイナリ (~80MB)
└── Info.plist
```

モデルは .app に同梱しない（配布サイズ削減）。初回起動ウィザードまたは設定画面から Hugging Face Hub のキャッシュへDL:
- Qwen3 8B: ~5GB (Ollama pull)
- Whisper medium: ~1.5GB (HuggingFace)

Whisper は設定画面で Tiny / Base / Small / Medium / Large v1 / Large v2 / Large v3 / Large v3 Turbo を選択できる。モデルのダウンロードは同時に1件だけ実行し、設定画面と録音開始後の画面が共通の進捗状態（取得済みバイト、総バイト、割合、エラー）を参照する。録音中のモデル削除・手動ダウンロードは受け付けない。

**起動シーケンス**:
```
1. Tauri 起動
2. 初回？ → セットアップウィザード
3. Python Backend sidecar 起動
4. /health 待ち → UI 表示
※ Ollama はパイプライン Phase 2 の LLM 使用直前に起動（録音中は不要、メモリ節約）
```

### 3.2 音声録音 + リアルタイムミックス

```
[native sidecar]                   [Python Backend]
ScreenCaptureKit                       sounddevice
  │                                      │
  │ raw PCM (float32, mono)              │ audio buffer
  │ + metadata JSON                      │
  ▼                                      ▼
raw file tail ───(raw PCM)────→ system_capture.py
(.meta.json で sample rate 共有)       │
                                         ▼
                                  ┌──────────────┐
                         mic ────→│ Realtime     │
                         sys ────→│ Mixer        │──→ Streaming Whisper
                                  │ (レベル正規化)│
                                  └──────┬───────┘
                                         │
                              ┌──────────┼──────────┐
                              ▼          ▼          ▼
                          mic.raw    system.raw → WebSocket → GUI
                          (個別保存)  (個別保存)   (文字起こし結果)
```

#### IPC プロトコル (native sidecar → Python)

- **トランスポート**: sidecar が `system.raw` に追記し、Python が raw を tail してリアルタイムミックスへ渡す
- **メタデータ**: `system.meta.json` に backend / format / sample_rate / channels を記録
- **フォーマット**: Raw PCM (`float32`, mono, little-endian, metadata の sample rate)
- **停止後変換**: Python 側が ffmpeg で `system.raw` を `system.wav` へ変換する
- **取得経路固定**: 内部音声は ScreenCaptureKit のみ。代替の内部音声取得経路は持たない

#### トラック同期

- リアルタイムミキサーは mic timeline を基準にし、system 欠損分は無音で埋める
- DB 保存用 transcript は録音中に Whisper へ投入した realtime mixed stream を正とする

#### ffmpeg の使用箇所

- **RAW → WAV 変換**: `ffmpeg -f s16le -ar 16000 -ac 1 -i mic.raw mic.wav`
- **リアルタイム補正**: Whisper 投入前に RMS ベースの自動ゲイン補正をかける
- **レベル補正**: Whisper 投入前のリアルタイムミックスに RMS ベースの自動ゲイン補正をかける
- **ミキシング**: Whisper に投入するリアルタイムミックス済み PCM を録音中に `combined.wav` へ保存し、停止後に `combined.flac` へ変換する
- ffmpeg バイナリは .app 内の `Resources/ffmpeg/` に同梱。subprocess で呼び出す

#### クラッシュ耐性

- RAW PCM で常にファイルに書き込み（WAVヘッダ不要→途中切断でもデータ有効）
- 正常停止時: `system.raw` → `system.wav` 変換 (ffmpeg)
- 異常終了後の再起動: RAW ファイル検出 → WAV 変換 → stopped_at を RAW ファイル末尾時刻で自動設定 → Phase 2 から再開

### 3.3 ストリーミング Whisper

```python
class StreamingWhisper:
    """録音中にリアルタイムで文字起こし"""

    OVERLAP_SEC = 1.0  # チャンク間オーバーラップ（文途切れ防止）

    def __init__(self, model_size="medium"):
        self.model = WhisperModel(model_size, device="auto")
        self.buffer = AudioRingBuffer()
        self.chunk_sec = 5.0
        self.offset_sec = 0.0       # 絶対時間への累積オフセット
        self.prev_tail = np.array([])  # 前チャンク末尾（オーバーラップ用）

    async def feed(self, mixed_chunk: np.ndarray) -> AsyncIterator[Segment]:
        """ミックス済み音声チャンクを受け取り文字起こし"""
        self.buffer.append(mixed_chunk)

        if self.buffer.duration >= self.chunk_sec:
            chunk = self.buffer.consume()

            # overlap_end を prev_tail 更新前に計算
            has_overlap = len(self.prev_tail) > 0
            overlap_end = self.OVERLAP_SEC if has_overlap else 0

            # 前チャンク末尾をオーバーラップとして先頭に付加
            if has_overlap:
                chunk_with_overlap = np.concatenate([self.prev_tail, chunk])
            else:
                chunk_with_overlap = chunk

            # 次回用にオーバーラップ分を保存
            overlap_samples = int(self.OVERLAP_SEC * 16000)
            self.prev_tail = chunk[-overlap_samples:]

            loop = asyncio.get_event_loop()
            segments_gen, _info = await loop.run_in_executor(
                None, self.model.transcribe, chunk_with_overlap
            )
            segments = list(segments_gen)

            # オーバーラップ区間の重複除去:
            # overlap 部分（先頭 OVERLAP_SEC）のセグメントは前チャンクで出力済み

            for seg in segments:
                # overlap 区間内で完結するセグメントはスキップ
                if seg.end <= overlap_end:
                    continue

                # 相対タイムスタンプ → 絶対時間に変換
                # (overlap 分を差し引いてから offset を加算)
                # 注: Segment は NamedTuple (immutable) なので _replace() で新オブジェクト生成
                new_start = max(seg.start, overlap_end) - overlap_end + self.offset_sec
                new_end = seg.end - overlap_end + self.offset_sec
                yield seg._replace(start=new_start, end=new_end)

            # オフセットを更新
            self.offset_sec += (len(chunk) / 16000)

    async def flush(self) -> list[Segment]:
        """録音停止後、残りバッファを処理"""
        if self.buffer.duration > 0:
            chunk = self.buffer.consume_all()
            if len(self.prev_tail) > 0:
                chunk = np.concatenate([self.prev_tail, chunk])
            loop = asyncio.get_event_loop()
            segments_gen, _info = await loop.run_in_executor(
                None, self.model.transcribe, chunk
            )
            segments = list(segments_gen)
            overlap_end = self.OVERLAP_SEC if len(self.prev_tail) > 0 else 0
            result = []
            for seg in segments:
                if seg.end <= overlap_end:
                    continue
                new_start = max(seg.start, overlap_end) - overlap_end + self.offset_sec
                new_end = seg.end - overlap_end + self.offset_sec
                result.append(seg._replace(start=new_start, end=new_end))
            return result
        return []

    def unload(self):
        del self.model
        gc.collect()
        # 注: faster-whisper は CTranslate2 ベースで CPU のみ動作（Metal/MPS 非対応）
        # torch.mps.empty_cache() は不要
```

**設計上の重要ポイント**:
- **オーバーラップ**: チャンク間に1秒の重複区間を設け、文の途中で切れることを防止。重複部分のセグメントは後処理で除去
- **リアルタイム表示**: ストリーミング結果を録音中に表示し、録音停止時に flush して未処理分を確定する
- **保存用タイムスタンプ**: ストリーミングチャンクごとの `offset_sec` を会議開始からの絶対時間として保持する
- **最終文字起こし**: DB に保存する transcript と要約入力は、録音中に確定したストリーミング transcript を正とする。`combined.flac` からの再文字起こしは手動実行時のみ行う

### 3.4 Context Agent

**Claude Code 的な自律調査エージェント + スライディングウィンドウ。**

#### コンテキストウィンドウ管理

Qwen3 8B のコンテキスト長は 32,768 tokens。15ステップのツール呼び出しで溢れるリスクがある。

```python
class SlidingWindowManager:
    """古いツール結果をトランケートして圧縮"""

    MAX_TOKENS = 28000  # 32K のうち 4K をシステムプロンプト+最新応答に確保
    TRUNCATE_CHARS = 500  # 圧縮時に残す先頭文字数

    def manage(self, messages: list[Message]) -> list[Message]:
        while count_tokens(messages) > self.MAX_TOKENS:
            # 最も古いツール結果をトランケート（LLM呼び出し不要で高速）
            oldest_tool_result = find_oldest_tool_result(messages)
            truncated = oldest_tool_result.content[:self.TRUNCATE_CHARS]
            replace_message(
                messages, oldest_tool_result,
                f"[以前の調査結果（先頭{self.TRUNCATE_CHARS}文字）: {truncated}...]"
            )
        return messages
```

#### 無限ループ検出

Qwen3 8B は tool calling の安定性が GPT-4o / Claude クラスに劣る場面がある。以下のガードレールを設ける:

```python
class LoopDetector:
    """同じツールを同じ引数で繰り返し呼ぶパターンを検出"""

    MAX_IDENTICAL_CALLS = 2  # 同一呼び出しの許容回数
    MAX_CONSECUTIVE_ERRORS = 3  # 連続エラーの許容回数

    def __init__(self):
        self.call_history: list[tuple[str, str]] = []  # (tool_name, args_hash)
        self.consecutive_errors = 0

    def check(self, tool_name: str, args: dict) -> bool:
        """True = ループ検出、強制終了すべき"""
        args_hash = hashlib.md5(json.dumps(args, sort_keys=True).encode()).hexdigest()
        call_key = (tool_name, args_hash)

        identical_count = self.call_history.count(call_key)
        if identical_count >= self.MAX_IDENTICAL_CALLS:
            return True

        self.call_history.append(call_key)
        return False

    def record_error(self) -> bool:
        """True = 連続エラー上限到達"""
        self.consecutive_errors += 1
        return self.consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS

    def record_success(self):
        self.consecutive_errors = 0
```

ループ検出時は調査を打ち切り、それまでに得られたコンテキストで議事録生成に進む。

**LLM による要約ではなくトランケートを採用**: 理由:
- ReAct ループ中に別の LLM 呼び出しを挟むと 1ステップあたり数秒のオーバーヘッドが発生
- 8B モデルの同時リクエスト処理は遅い
- 先頭500文字のトランケートでも、ファイルパスや構造の概要は十分保持される
- 最新のツール結果は常に全文保持されるため、直近の調査精度は維持

#### ツール定義

| ツール | 引数 | 説明 | セキュリティ |
|--------|------|------|------------|
| `read_file` | `path, offset?, limit?` | ファイル読み取り（セクション分割対応） | repo_path + doc_dirs 内のみ |
| `list_dir` | `path` | ディレクトリ一覧 | 同上 |
| `search` | `query, glob?, path?` | ripgrep テキスト検索 | 同上 |
| `git_log` | `n` | 直近N件のコミットログ | repo_path のみ |
| `git_diff` | `ref?` | 差分表示 | repo_path のみ |
| `git_branch` | - | ブランチ一覧 | repo_path のみ |
| `run_python` | `code` | Python 実行 | subprocess + 30s timeout + 書き込み禁止 |

#### run_python のサンドボックス

```python
# 禁止パターン: import/実行が危険なモジュール
BLOCKED_IMPORTS = {"os", "subprocess", "shutil", "pathlib", "socket", "http", "urllib",
                   "ftplib", "smtplib", "ctypes", "multiprocessing", "signal"}
BLOCKED_PATTERNS = [r"\bopen\s*\(", r"\b__import__\b", r"\bexec\s*\(", r"\beval\s*\(",
                    r"\bcompile\s*\(", r"\bglobals\s*\(", r"\bgetattr\s*\("]

def validate_code(code: str) -> tuple[bool, str]:
    """静的解析で危険なコードを拒否"""
    import ast, re
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {alias.name.split(".")[0] for alias in node.names} if isinstance(node, ast.Import) \
                    else {node.module.split(".")[0]} if node.module else set()
            blocked = names & BLOCKED_IMPORTS
            if blocked:
                return False, f"Blocked import: {blocked}"

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, code):
            return False, f"Blocked pattern: {pattern}"

    return True, ""

def execute_python(code: str) -> str:
    """subprocess で隔離実行。事前バリデーション + stdout のみ返却"""
    ok, reason = validate_code(code)
    if not ok:
        return f"[実行拒否] {reason}"

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": "",  # PATH を空にして外部コマンド実行を抑制
            "HOME": "/tmp/seam-sandbox",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "",  # 追加モジュール読み込みを抑制
        },
        cwd="/tmp/seam-sandbox",
    )
    return result.stdout[:10000]
```

**セキュリティ方針**:
- **静的解析によるバリデーション**: AST パースで危険な import/関数呼び出しを事前ブロック
- `PATH` を空にして外部コマンド実行を抑制
- `HOME` と `cwd` を `/tmp/seam-sandbox` に設定
- **sandbox-exec は不採用**: macOS 10.15 以降 deprecated。将来削除される可能性があり、プロダクションでの依存は不適切
- **リスク受容**: AST ベースの静的解析は完全ではない（`__builtins__` 経由等の迂回手段あり）。ただしローカル LLM (Qwen3) が生成するコードが攻撃的である可能性は低い。会議音声経由のプロンプトインジェクションは理論的リスクとして認識
- **v2 以降**: XPC Service による完全なプロセス分離を検討

**注意**: PyInstaller は `--onedir` モードを使用。`--onefile` では `sys.executable` が
一時展開先を指すため subprocess で問題が起きる。`--onedir` なら正常に動作する。

### 3.6 Minutes Agent (議事録生成)

#### 話題ベースのチャンク分割

```
話題検出の優先順位:
1. 長い沈黙 (3秒以上の無音区間)
2. 明示的な話題転換フレーズ ("次の議題", "じゃあ次", "それでは" 等)
3. 時間上限 (30分で強制分割)

短すぎるチャンク (5分未満) は前のチャンクにマージ
```

#### 段階的要約

```
                    コンテキスト (プロジェクト情報)
                            │
全文書き起こし                │
    │                       │
    ├── チャンク1 ──→ LLM ──→ 部分要約1
    ├── チャンク2 ──→ LLM ──→ 部分要約2
    └── チャンクN ──→ LLM ──→ 部分要約N
                              │
                全部分要約を結合 + コンテキスト
                              │
                       LLM ──→ 最終議事録 (固定Markdown)
```

### 3.7 Pipeline Orchestrator

#### 状態遷移

```python
class PipelineStage(Enum):
    RECORDING = "recording"
    FLUSHING_WHISPER = "flushing_whisper"
    CONVERTING_WAV = "converting_wav"
    MIXING = "mixing"
    GENERATING_MINUTES = "generating_minutes"
    SAVING = "saving"
    CLEANING_UP = "cleaning_up"
    COMPLETED = "completed"
    FAILED = "failed"
```

#### state.json

```json
{
  "session_id": "20260403_143000_abc123",
  "project_id": "egaite",
  "current_stage": "generating_minutes",
  "started_at": "2026-04-03T14:30:00",
  "stopped_at": "2026-04-03T15:15:00",
  "duration_sec": 2700,
  "devices": { ... },
  "stages": {
    "recording": { "status": "completed", "completed_at": "..." },
    "flushing_whisper": { "status": "completed", "completed_at": "..." },
    "converting_wav": { "status": "completed", "completed_at": "..." },
    "mixing": { "status": "completed", "completed_at": "..." },
    "generating_minutes": { "status": "in_progress" },
    ...
  }
}
```

`current_stage` フィールドにより、途中再開時に即座にどこから再開すべきかを判定。

#### セッション作業ディレクトリ

```
~/.seam/sessions/{session_id}/
├── state.json                   # パイプライン状態
├── mic.raw → mic.wav            # ← 完了後に全削除
├── system.raw → system.wav
├── combined.flac                # ←
├── streaming_transcript.json    # ←
├── context.json                 # ←
└── minutes.md                   # ←
```

**パイプライン完了後、セッションディレクトリを丸ごと削除。**
正本は SQLite (`minutes.db`) と output_dir の Markdown ファイル。

---

## 4. 通信設計

### 4.1 REST API

| Method | Path | 説明 |
|--------|------|------|
| GET | /api/projects | プロジェクト一覧 |
| POST | /api/projects | プロジェクト作成 |
| PUT | /api/projects/{id} | プロジェクト更新 |
| DELETE | /api/projects/{id} | プロジェクト削除（output_dir 削除は確認パラメータで制御） |
| GET | /api/devices | オーディオデバイス一覧 |
| POST | /api/recording/start | 録音開始 |
| POST | /api/recording/stop | 録音停止 |
| GET | /api/recording/status | 録音状態取得 |
| GET | /api/minutes?project={id}&limit=20&offset=0 | 議事録一覧（日付降順、ページネーション対応） |
| GET | /api/minutes/{id} | 議事録詳細 |
| GET | /api/minutes/{id}/transcript | 全文書き起こし |
| PUT | /api/minutes/{id}/project | プロジェクト再割り当て（DB移動 + Markdown 移動） |
| DELETE | /api/minutes/{id} | 議事録削除（DB + output_dir の Markdown も削除） |
| GET | /api/minutes/search?q={query}&project={id} | 議事録検索 |
| GET | /api/settings | 設定取得 |
| PUT | /api/settings | 設定更新 |
| GET | /api/models/whisper | Whisperモデル一覧・キャッシュ状態・ダウンロード進捗 |
| POST | /api/models/whisper/{model_name}/download | Whisperモデルのダウンロード開始 |
| DELETE | /api/models/whisper/{model_name} | Whisperモデルのキャッシュ削除 |
| GET | /health | ヘルスチェック |

### 4.2 エラーレスポンス形式

全 API で統一:
```json
{
  "error": {
    "code": "PROJECT_NOT_FOUND",
    "message": "プロジェクト 'egaite' が見つかりません",
    "details": null
  }
}
```
- 400: バリデーションエラー
- 404: リソース未発見
- 409: 競合（パイプライン実行中に録音開始等）
- 500: 内部エラー

### 4.3 WebSocket

```
ws://localhost:18900/ws

// リアルタイム文字起こし (Phase 1)
{ "type": "transcript", "data": { "text": "...", "start": 123.5, "end": 125.3 } }

// 音声レベル (Phase 1)
{ "type": "audio_level", "data": { "mic": 0.72, "system": 0.45 } }

// 録音状態 (Phase 1)
{ "type": "recording_status", "data": { "state": "recording", "elapsed_sec": 123 } }

// Whisperモデル準備状態 (録音開始時)
{ "type": "streaming_status", "data": {
    "model_state": "loading",
    "model_name": "medium",
    "model_download": {
      "state": "downloading",
      "current_bytes": 524288000,
      "total_bytes": 1572864000,
      "percent": 33.3
    }
} }

// パイプライン進捗 (Phase 2)
{ "type": "pipeline_progress", "data": {
    "stage": "generating_minutes",
    "progress": 0.65,
    "message": "議事録生成中..."
} }

// 完了
{ "type": "minutes_completed", "data": { "minutes_id": "...", "project_id": "..." } }

// エラー
{ "type": "error", "data": { "stage": "...", "message": "...", "recoverable": true } }
```

**GUI 側の再接続戦略**:
- Python Backend 再起動時に WebSocket が切断される
- 指数バックオフで自動再接続（1s, 2s, 4s, 8s, max 30s）
- 録音中に切断→再接続した場合: サーバー側は文字起こし結果をバッファリング、再接続後にまとめて送信
- パイプライン進捗は再接続後にサーバーの現在状態を GET /api/recording/status で取得して UI を復元

---

## 5. エラーハンドリング

| 状況 | 対応 |
|------|------|
| Ollama 未起動 | Python Backend (startup.py) が Phase 2 開始時に自動起動。失敗時はエラー表示 |
| モデル未DL | セットアップウィザードへ誘導 |
| Python Backend 起動失敗 | 3回リトライ → エラー表示 |
| ScreenCaptureKit 権限/初期化失敗 | 内部音声録音を開始失敗扱いにし、システム音声取得権限と sidecar 配置を確認する |
| マイクデバイス未検出 | デバイス一覧を表示して選択を促す |
| sidecar 停止 (native↔Python) | RAW PCM はファイルに保持。停止後に coverage を検証し、内部音声欠落を正常完了扱いしない |
| 内部音声の全時間無音 | ScreenCaptureKit の RAW bytes はあるが非ゼロ音声が検出できない場合、内部音声欠落として正常完了扱いしない |
| 録音中クラッシュ | RAW PCM 保持 → 再起動後に検出 → stopped_at を RAW ファイル末尾時刻で自動設定 → Phase 2 から再開提案 |
| Tauri 終了 (録音後処理中) | Python Backend に SIGTERM → state.json を保存して graceful shutdown → 再起動後に current_stage から再開 |
| Whisper OOM | 小さいモデルへのフォールバック提案 |
| LLM コンテキスト超過 | スライディングウィンドウで自動圧縮 |
| 長い書き起こし | チャンク分割 + 段階的要約 |
| コンテキスト調査タイムアウト | 60秒で打ち切り、それまでの知識で議事録生成 |
| output_dir が存在しない | 自動作成 |
| output_dir 書き込み失敗 | エラー表示 + DB には保存済みなのでデータロスなし |
