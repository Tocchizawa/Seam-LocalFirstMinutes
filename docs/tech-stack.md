# Seam - 技術選定書

## 1. 技術スタック一覧

| カテゴリ | 技術 | バージョン | 用途 |
|----------|------|-----------|------|
| **GUI** | Tauri | 2.x | デスクトップアプリ + ScreenCaptureKit + プロセス管理 |
| **GUI フロント** | React + TypeScript | 19+ | UI |
| **GUI スタイル** | Tailwind CSS | 4+ | スタイリング |
| **バックエンド** | Python | 3.12+ | AI処理・API |
| **API** | FastAPI | 0.115+ | REST + WebSocket |
| **パッケージ管理 (Python)** | uv | latest | 高速な依存管理 |
| **パッケージ管理 (GUI)** | pnpm | 9+ | フロント依存管理 |
| **音声認識** | faster-whisper | 1.0+ | ストリーミング文字起こし |
| **LLM** | Ollama + Qwen3 8B | latest | コンテキスト調査・議事録生成 |
| **マイク録音** | sounddevice | 0.5+ | マイク入力 |
| **内部音声** | ScreenCaptureKit | macOS 13+ | システム音声キャプチャ (Swift sidecar) |
| **内部音声 (FB)** | BlackHole | 0.6+ | フォールバック (Python完結) |
| **音声処理** | ffmpeg | 7+ | RAW→WAV変換・レベル正規化・2トラックミキシング (.app同梱) |
| **ファイル読取** | pymupdf | 1.24+ | PDF (Context Agent) |
| **ファイル読取** | python-docx | 1.1+ | Word (Context Agent) |
| **ファイル読取** | openpyxl | 3.1+ | Excel (Context Agent) |
| **DB** | SQLite (標準) | 3.35+ | FTS5 trigram で日本語全文検索 |
| **設定** | PyYAML | 6+ | YAML |
| **HTTP** | httpx | 0.27+ | Ollama API |
| **バンドル** | PyInstaller | 6+ | Python→バイナリ |

---

## 2. 選定理由

### 2.1 Tauri

3つの役割を1つで担える:
1. **GUI**: 軽量 (~10MB)、メモリを AI に回せる
2. **Sidecar管理**: Python Backend の起動・監視・停止（Ollama は Python が Phase 2 で起動）
3. **ScreenCaptureKit**: Rust から macOS ネイティブ API に直接アクセス

### 2.2 FastAPI

- WebSocket でリアルタイム文字起こしを GUI にプッシュ
- async でパイプラインの非同期実行
- Whisper の `transcribe()` は同期関数だが `run_in_executor` で共存

### 2.3 ScreenCaptureKit (Swift sidecar)

- ドライバ不要（BlackHole 不要）
- アプリ単位で音声キャプチャ可能
- **Swift sidecar バイナリ**として実装（Tauri の sidecar 機能で起動）
  - Rust の SCK バインディング (`screencapturekit-rs` 等) は未成熟。特に audio-only streaming が不安定
  - Swift は SCK のネイティブ API に直接アクセスでき、デリゲートパターンも自然に扱える
  - Tauri `tauri.conf.json` の sidecar 設定でバンドル・起動管理
- Unix domain socket (`/tmp/seam-audio-{pid}.sock`) で Python に RAW PCM を転送
  - SCK デフォルト出力は **48kHz** → Swift 側で **16kHz にリサンプリング**してから送出
  - 固定フォーマット: 16kHz, mono, int16LE
  - ヘッダなし、フロー制御はカーネルバッファに依存
  - 切断時は Swift 側がバッファリング + RAW ファイルに常に書いているのでロスなし
- **audio-only でも画面収録権限が必要**（macOS の制約）。初回ウィザードでガイド

BlackHole フォールバック時は Python (sounddevice) 完結で Swift sidecar 不要。

### 2.4 faster-whisper

- ストリーミング: 数秒チャンクごとにリアルタイム文字起こし
- **1秒オーバーラップ**: チャンク間に重複区間を設けて文の途中切れを防止
- **絶対タイムスタンプ**: チャンクごとに offset を累積加算
- **最終補正は行わない**: ストリーミング結果 + 残バッファ flush が最終結果
- **CPU のみ対応** (CTranslate2 ベース): CUDA / CPU のみ。**Metal/MPS は非対応**
  - Apple Silicon では ARM NEON 最適化による CPU 推論で動作。medium モデルで実用的な速度
  - GPU 推論が必要な場合の代替: **mlx-whisper** (Apple MLX) または **whisper.cpp** (Metal 対応)
  - v1 は faster-whisper (CPU) で開始し、速度が不足する場合に mlx-whisper を検討
- VAD 内蔵で無音スキップ
- **注意**: `transcribe()` が返す Segment は **NamedTuple (immutable)**。`seg.start = ...` は不可。`seg._replace(start=..., end=...)` で新オブジェクトを生成する必要あり

### 2.5 Ollama + Qwen3 8B

- 日本語最強クラス + tool calling 対応
- Whisper 完了後にロード（メモリ競合回避）
- コンテキストウィンドウ 32K tokens
  - Context Agent はスライディングウィンドウで古い結果をトランケート（先頭500文字）
  - Minutes Agent はチャンク分割 + 段階的要約

### 2.6 SQLite + FTS5 trigram

- `tokenize='trigram'`: 日本語の部分一致検索を追加ライブラリなしで実現
- 3文字 n-gram インデックス。形態素解析より粗いが、導入コストゼロで安定

---

## 3. 配布・インストール

### 3.1 .dmg の内容

```
GijirokuN.app/Contents/
├── MacOS/Seam              # Tauri メインバイナリ (~10MB)
├── Resources/
│   ├── backend/                 # PyInstaller --onedir 出力
│   │   ├── seam-backend     # エントリーポイント
│   │   └── _internal/           # Python + 依存 (~3GB)
│   ├── sidecar/
│   │   └── audio-capture        # Swift sidecar (ScreenCaptureKit)
│   ├── ollama/ollama            # Ollama バイナリ (~200MB)
│   └── ffmpeg/ffmpeg            # ffmpeg バイナリ (~80MB)
└── Info.plist
```

**モデルは同梱しない**。初回起動時にダウンロード:
- Qwen3 8B: ~5GB (Ollama pull)
- Whisper medium: ~1.5GB (HuggingFace)

.dmg サイズ: ~3.5GB（Python バックエンド + Ollama + ffmpeg）
初回DL合計: ~6.5GB

### 3.2 初回起動ウィザード

```
1. Ollama 確認
2. Qwen3 8B ダウンロード → プログレスバー
3. Whisper medium ダウンロード → プログレスバー
4. macOS 権限リクエスト:
   → マイクアクセス
   → 画面収録 (ScreenCaptureKit 用)
5. 完了 → メイン画面
```

### 3.3 PyInstaller

```bash
pyinstaller \
  --onedir \
  --name seam-backend \
  --hidden-import faster_whisper \
  --collect-all faster_whisper \
  src/main.py
```

`--onedir` は `--onefile` より起動が速く、`sys.executable` で
subprocess (run_python ツール) が正常動作する。UPX 圧縮で多少削減可能。

---

## 4. 開発ツール

| ツール | 用途 |
|--------|------|
| ruff | Python リンター + フォーマッター |
| mypy | Python 型チェック |
| pytest + pytest-asyncio | テスト |
| biome | TypeScript リンター + フォーマッター |
| Vitest | フロントエンドテスト |

---

## 5. プロジェクト構成

```
Seam/
├── docs/                    # 設計ドキュメント
├── gui/                     # Tauri + React
│   ├── src-tauri/           # Rust
│   │   └── src/
│   │       ├── main.rs
│   │       └── sidecar.rs   # Python Backend + Swift sidecar 起動管理
│   ├── sidecar/
│   │   └── audio-capture/   # Swift sidecar (ScreenCaptureKit)
│   │       ├── Package.swift
│   │       └── Sources/main.swift
│   ├── src/                 # React
│   │   ├── pages/
│   │   ├── components/
│   │   └── lib/
│   ├── package.json
│   └── tailwind.config.ts
├── src/                     # Python バックエンド
│   ├── main.py
│   ├── api/
│   ├── project/
│   ├── context/
│   ├── audio/
│   ├── transcribe/
│   ├── summarize/
│   ├── pipeline/
│   ├── storage/
│   └── llm/
├── installer/               # ビルド・パッケージング
├── pyproject.toml
└── README.md
```

---

## 6. リスクと対策

| リスク | 影響 | 対策 |
|--------|------|------|
| PyInstaller バンドル ~3GB | 初回DLが大きい | UPX圧縮 + 不要依存除外 |
| ScreenCaptureKit macOS 13+ 限定 | 古い macOS 非対応 | BlackHole フォールバック (Python完結) |
| SCK の Swift sidecar 実装 | PoC が必要。リサンプリング (48kHz→16kHz) のレイテンシ | Swift sidecar をメインパスに採用。PoC を最優先で実施。BlackHole フォールバック |
| Swift↔Python IPC 切断 | 音声データロス | RAW PCM を常にファイル書き込み。ロスなし |
| ストリーミング Whisper 遅延蓄積 | 文字起こしが遅れる | チャンク長動的調整 + 1秒オーバーラップ + run_in_executor |
| 3時間会議で Qwen3 コンテキスト超え | 議事録品質低下 | 話題ベースチャンク分割 + 段階的要約 |
| Context Agent が 32K tokens 超え | 調査が中途半端 | スライディングウィンドウで古い結果をトランケート（先頭500文字） |
| run_python で悪意あるコード実行 | セキュリティリスク | subprocess + timeout + 引数バリデーション + import 制限。sandbox-exec は macOS 10.15 以降 deprecated のため不採用。v2 で XPC Service 分離を検討 |
| Ollama tool calling 仕様変更 | エージェント破壊 | LLM クライアント層で吸収 |
