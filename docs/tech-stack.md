# Seam - 技術選定書

## 1. 技術スタック一覧

| カテゴリ | 技術 | バージョン | 用途 |
|----------|------|-----------|------|
| **GUI** | Tauri | 2.x | デスクトップアプリ + native sidecar + プロセス管理 |
| **GUI フロント** | React + TypeScript | 19+ | UI |
| **GUI スタイル** | Tailwind CSS | 4+ | スタイリング |
| **バックエンド** | Python | 3.12+ | AI処理・API |
| **API** | FastAPI | 0.115+ | REST + WebSocket |
| **パッケージ管理 (Python)** | uv | latest | 高速な依存管理 |
| **パッケージ管理 (GUI)** | pnpm | 9+ | フロント依存管理 |
| **音声認識** | faster-whisper | 1.0+ | ストリーミング文字起こし |
| **LLM** | Ollama + Qwen3 8B | latest | コンテキスト調査・議事録生成 |
| **マイク録音** | Core Audio Audio Queue | macOS | マイク入力 (Objective-C sidecar) |
| **内部音声** | ScreenCaptureKit audio stream | macOS 13.0+ | システム音声キャプチャ (Objective-C sidecar) |
| **音声処理** | ffmpeg | 7+ | mixed WAV→FLAC 変換 (.app同梱) |
| **ファイル読取** | pymupdf | 1.24+ | PDF (Context Agent) |
| **ファイル読取** | python-docx | 1.1+ | Word (Context Agent) |
| **ファイル読取** | openpyxl | 3.1+ | Excel (Context Agent) |
| **DB** | SQLite (標準) | 3.35+ | FTS5 trigram で日本語全文検索 |
| **設定** | PyYAML | 6+ | YAML |
| **HTTP** | httpx | 0.27+ | Ollama API |
| **バンドル** | PyInstaller | 6+ | Python→バイナリ |
| **更新配布** | Tauri Updater | 2.x | 署名済み `.app.tar.gz` によるアプリ内更新 |

---

## 2. 選定理由

### 2.1 Tauri

3つの役割を1つで担える:
1. **GUI**: 軽量 (~10MB)、メモリを AI に回せる
2. **Sidecar管理**: Python Backend の起動・監視・停止（Ollama は Python が Phase 2 で起動）
3. **Sidecar管理**: native audio sidecar と Python Backend の起動に必要なリソース/環境変数を管理

### 2.2 FastAPI

- WebSocket でリアルタイム文字起こしを GUI にプッシュ
- async でパイプラインの非同期実行
- Whisper の `transcribe()` は同期関数だが `run_in_executor` で共存

### 2.3 音声キャプチャ (native audio sidecar)

- マイク入力は **Audio Queue** を使い、Objective-C sidecar が RAW PCM (`float32`, mono, 16kHz) を書き出す。Python は raw を追尾して `mic.wav` とリアルタイムミックスへ流す。
- 内部音声は **ScreenCaptureKit audio stream** を使う。`SCStreamOutputTypeAudio` を Objective-C sidecar で受け取り、mono `float32` RAW PCM と metadata JSON を書き出す。
- **Objective-C sidecar バイナリ**として実装し、Tauri の resources に同梱する。
- sidecar は `system.raw` (`float32`, mono, requested sample rate) と `system.meta.json` を書き出す。Python は raw を追尾してリアルタイムミックスへ流し、停止時に ffmpeg で `system.wav` へ変換する。
- 内部音声の取得経路は ScreenCaptureKit のみ。macOS 13.0 未満、sidecar 起動失敗、ScreenCaptureKit 権限/初期化失敗時は内部音声録音を開始失敗として扱う。
- システム音声取得用に `NSAudioCaptureUsageDescription` を Info.plist に含める。

### 2.4 faster-whisper

- ストリーミング: 数秒チャンクごとにリアルタイム文字起こし
- **1秒オーバーラップ**: チャンク間に重複区間を設けて文の途中切れを防止
- **絶対タイムスタンプ**: チャンクごとに offset を累積加算
- **最終結果**: ストリーミング結果を録音停止時に flush し、DB 保存用 transcript として確定する。`combined.flac` からの再文字起こしは手動実行時のみ行う
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
│   ├── audio-capture            # Objective-C sidecar (ScreenCaptureKit + Core Audio mic)
│   ├── ollama/ollama            # Ollama バイナリ (~200MB)
│   └── ffmpeg/ffmpeg            # ffmpeg バイナリ (~80MB)
└── Info.plist
```

**モデルは同梱しない**。初回起動時にダウンロード:
- Qwen3 8B: ~5GB (Ollama pull)
- Whisper medium: ~1.5GB (HuggingFace)

.dmg サイズ: ~3.5GB（Python バックエンド + Ollama + ffmpeg）
初回DL合計: ~6.5GB

### 3.1.1 アプリ内アップデート

- 通常インストール用の DMG に加えて、Tauri updater 用の `Seam.app.tar.gz` / `Seam.app.tar.gz.sig` を GitHub Releases にアップロードする
- アプリは固定フィード `updater-feed/latest.json` を参照し、`darwin-aarch64` の署名付き artifact を取得する
- 起動時は設定に従って更新確認を行う。`auto_install_on_startup=true` の場合のみ、自動でダウンロード・インストール・再起動まで進める
- 更新署名の秘密鍵はリポジトリに含めず、ローカルまたは GitHub Actions secrets の `TAURI_SIGNING_PRIVATE_KEY` で管理する

### 3.2 初回起動ウィザード

```
1. Ollama 確認
2. Qwen3 8B ダウンロード → プログレスバー
3. Whisper medium ダウンロード → プログレスバー
4. macOS 権限リクエスト:
   → マイクアクセス
   → システム音声取得
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
│   │       └── sidecar.rs   # Python Backend + native sidecar 起動管理
│   ├── sidecar/
│   │   └── audio-capture/   # Objective-C sidecar (ScreenCaptureKit + Core Audio mic)
│   │       └── Sources/main.m
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
| ScreenCaptureKit macOS 13.0+ 限定 | 古い macOS 非対応 | 内部音声を要求した録音は開始失敗にし、OS/権限の確認を促す |
| システム音声 sidecar 停止/無音化 | 相手音声が途中から消える | 開始失敗・空ファイル・coverage 不足を正常完了扱いにしない |
| native sidecar 停止 | 音声データロス | RAW PCM を常にファイル書き込みし、Python 側で coverage を検証 |
| ストリーミング Whisper 遅延蓄積 | 文字起こしが遅れる | チャンク長動的調整 + 1秒オーバーラップ + run_in_executor |
| 3時間会議で Qwen3 コンテキスト超え | 議事録品質低下 | 話題ベースチャンク分割 + 段階的要約 |
| Context Agent が 32K tokens 超え | 調査が中途半端 | スライディングウィンドウで古い結果をトランケート（先頭500文字） |
| run_python で悪意あるコード実行 | セキュリティリスク | subprocess + timeout + 引数バリデーション + import 制限。sandbox-exec は macOS 10.15 以降 deprecated のため不採用。v2 で XPC Service 分離を検討 |
| Ollama tool calling 仕様変更 | エージェント破壊 | LLM クライアント層で吸収 |
