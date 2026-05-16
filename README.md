# Seam - Local-First Minutes

Seam は、**会議の録音・文字起こし・要約をローカル環境中心で完結**させる macOS 向けアプリです。  
デスクトップUI（Tauri + React）とバックエンドAPI（FastAPI）を分離し、実運用しやすい構成で実装しています。

## プロジェクトの狙い

- ローカルで高速に議事録を作れること
- 会議データの取り扱いをシンプルにできること
- 1人開発でも保守しやすい構成にすること

## 主な機能（実装ベース）

### 1. 録音・リアルタイム文字起こし

- マイク録音 + システム音声録音（任意）を同時に実行
- 録音中にリアルタイムで文字起こしを表示
- VAD（`silero` / `energy`）で発話区間を分割して安定処理
- 録音中のマイクソフトミュート切り替え
- 録音停止忘れ通知（無音が続いた場合）

### 2. 議事録管理

- 録音停止後に議事録を自動保存（SQLite）
- 要約と全文書き起こしを同じ議事録で管理
- タイトル編集・要約編集・削除・プロジェクト再割り当て
- 議事録を Markdown でエクスポート（要約 + 全文）
- 保存済み音声から文字起こしを再実行
- 全文検索（SQLite FTS5 trigram）

### 3. 話者管理

- 話者分離方式の切り替え（`legacy` / `pyannote`）
- 話者プロファイルの一覧・リネーム・統合・削除
- 話者サンプル音声の再生
- `pyannote` 用の HuggingFace トークン保存 / テスト

### 4. 要約（複数プロバイダ）

- Ollama（ローカル）
- Claude API
- OpenAI API
- Claude Code CLI
- Codex CLI
- 録音停止時の自動要約、または議事録ごとの手動再要約
- 要約後の辞書更新（用語集 / 誤転写補正）を自動化

### 5. プロジェクト運用

- プロジェクトの作成・更新・削除
- プロジェクトごとの出力先ディレクトリ管理
- コンテキスト参照用の `repo_path` / `doc_dirs` 設定
- 用語集と誤転写補正ルール（`wrong` → `correct`）の管理
- ドキュメント群からの用語集候補の自動抽出

## 実装状況メモ

- `Gemini` は設定UI/APIの枠はありますが、要約本体は未対応です
- システム音声録音は macOS の画面収録権限が必要です
- ローカル完結運用が基本ですが、Claude/OpenAI などクラウド要約も選択可能です

## 技術スタック

- Desktop: Tauri + React + TypeScript
- Backend: FastAPI (Python)
- AI/ML: mlx-whisper / Silero VAD / pyannote / Ollama / 外部LLM
- Storage: SQLite + YAML + Keyring
- Build: pnpm / uv / Rust toolchain

詳細は [docs/tech-stack.md](./docs/tech-stack.md) を参照してください。

## ローカル実行（開発）

前提:

- macOS
- Python 3.11+
- Node.js 20+
- `pnpm` 9+
- Rust (stable)
- `uv`
- `ffmpeg`

```bash
uv sync
pnpm -C gui install
pnpm dev
```

`pnpm dev` で Tauri 開発モードが起動し、バックエンドはアプリ側から起動します。

## ビルド

```bash
pnpm build
```

生成物:

- `gui/src-tauri/target/release/bundle/macos/Seam.app`
- `gui/src-tauri/target/release/bundle/dmg/Seam_0.1.0_aarch64.dmg`

## セキュリティ / プライバシー方針（要点）

- バックエンドは `127.0.0.1` バインドが既定
- `/api/*` はローカル接続のみ許可（必要時のみ設定で緩和）
- 更新系APIは Origin も検証
- APIキーは macOS Keyring に保存

詳細は [SECURITY.md](./SECURITY.md) を参照してください。

## 設定の優先順（UIと環境変数）

1. 設定画面で保存した値
2. Keyring に保存したトークン
3. 環境変数

保存先:

- 一般設定: `~/.seam/config.yaml`
- APIキー / HFトークン: macOS Keyring（`seam-app`）

参照する主な環境変数:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `HF_TOKEN`
- `HUGGING_FACE_HUB_TOKEN`
- `OLLAMA_HOST`

`.env` は自動読込しません。必要時に `export` して使います。  
ひな型: [`.env.example`](./.env.example)

## ディレクトリ構成

```text
.
├── docs/
├── gui/
├── scripts/
├── src/
├── tests/
└── sidecar/
```

## 最低限の検証

```bash
./scripts/oss_preflight.sh
```

高速チェック（GUI build 省略）:

```bash
SKIP_GUI_BUILD=1 ./scripts/oss_preflight.sh
```

## ドキュメント

- 要件: [docs/requirements.md](./docs/requirements.md)
- 設計: [docs/architecture.md](./docs/architecture.md)
- データモデル: [docs/data-model.md](./docs/data-model.md)
- 要約設計: [docs/summarize-design.md](./docs/summarize-design.md)

## 開発・運用

- コントリビュート: [CONTRIBUTING.md](./CONTRIBUTING.md)
- セキュリティ: [SECURITY.md](./SECURITY.md)
- ライセンス: [MIT](./LICENSE)
