# Seam

Seam は、macOS で会議音声の録音から文字起こし・要約までをローカルで回すアプリです。  
フロントエンドは Tauri + React、バックエンドは FastAPI で構成しています。

## できること

- マイク/システム音声の録音
- ストリーミング文字起こし（Whisper）
- 話者分離と話者名の記憶
- 議事録要約（Ollama / Claude API / OpenAI API）
- `.app` / `.dmg` での配布

## 開発環境

- macOS
- Python 3.11+
- Node.js 20+
- `pnpm` 9+
- Rust (stable)
- `uv`
- `ffmpeg`

## 開発の始め方

```bash
uv sync
pnpm -C gui install
pnpm dev
```

`pnpm dev` で Tauri 開発モードが立ち上がり、バックエンドはアプリ側から起動します。

## ビルド

```bash
pnpm build
```

生成物:

- `gui/src-tauri/target/release/bundle/macos/Seam.app`
- `gui/src-tauri/target/release/bundle/dmg/Seam_0.1.0_aarch64.dmg`

## 設定について（UIと環境変数の使い分け）

このアプリは、基本的に設定画面で完結する想定です。  
環境変数は「CLI検証用」や「CI注入用」のフォールバックとして扱っています。

値の解決順は次のとおりです。

1. 設定画面で保存した値
2. Keyring に保存したトークン
3. 環境変数

保存先:

- 一般設定: `~/.seam/config.yaml`
- APIキー / HFトークン: macOS Keyring（`seam-app`）

現在参照している環境変数:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `HF_TOKEN`
- `HUGGING_FACE_HUB_TOKEN`
- `OLLAMA_HOST`（接続先を上書きしたい場合）

`.env` は自動読込しません。使う場合はシェルで `export` してください。  
変数のひな型は [`.env.example`](./.env.example) にあります。

## リポジトリ構成

```text
.
├── docs/
├── gui/
├── scripts/
├── src/
├── tests/
└── sidecar/
```

## 最低限のチェック

```bash
uv run python -m compileall src
pnpm -C gui build
```

## 個人開発向けの軽量OSS運用

公開前は次の1コマンドだけ回せば十分です。

```bash
./scripts/oss_preflight.sh
```

速く確認したいときは GUI build を省略できます。

```bash
SKIP_GUI_BUILD=1 ./scripts/oss_preflight.sh
```

## 補足

- コントリビュート: [CONTRIBUTING.md](./CONTRIBUTING.md)
- セキュリティ: [SECURITY.md](./SECURITY.md)
- ライセンス: [MIT](./LICENSE)
