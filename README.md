# Seam - Local-First Minutes

Seam は、**会議の録音・文字起こし・要約をローカル環境中心で完結**させる macOS 向けアプリです。  
デスクトップUI（Tauri + React）とバックエンドAPI（FastAPI）を分離し、実運用しやすい構成で実装しています。

## プロジェクトの狙い

- ローカルで高速に議事録を作れること
- 会議データの取り扱いをシンプルにできること
- 1人開発でも保守しやすい構成にすること

## 主な機能

- マイク / システム音声の録音
- Whisper によるストリーミング文字起こし
- 話者分離と話者ラベル記憶
- 議事録要約（Ollama / Claude API / OpenAI API）
- 議事録の検索・編集・エクスポート
- `.app` / `.dmg` での配布

## 技術スタック

- Desktop: Tauri + React + TypeScript
- Backend: FastAPI (Python)
- AI/ML: Whisper / Ollama / 外部LLM API
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
