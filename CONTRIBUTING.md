# Contributing Guide

このプロジェクトへの貢献ありがとうございます。  
変更前に、以下の最小ルールを確認してください。

## 1. 言語運用方針

このリポジトリは **日本語ベース運用 + 公開面は英語** を基本方針にします。

- 日常運用（Issue / PR本文 / レビューコメント / 作業コミット）: 日本語
- `main` に残る公開面（PRタイトル / マージコミット / リリースノート）: 英語
- ドキュメント: 日本語（必要な固有名詞・コマンド・識別子は英語のまま）

コミットメッセージ例:

- 作業コミット（日本語）: `CI: Tauriビルド前にバックエンド同梱物を生成`
- `main` 向け（英語）: `ci: bundle backend resources before tauri build`

## 2. 開発環境セットアップ

```bash
uv sync
pnpm -C gui install
```

## 3. ブランチ

- `main` / `develop` へ直接 push せず、作業ブランチで開発してください。
- 通常の機能追加・修正は `develop` から `feature/*` / `fix/*` を作成し、PR も `develop` 向けにしてください。
- リリース時は `origin/develop` から `release/vX.Y.Z` を作成し、release PR として `main` に入れます。
- 緊急修正だけ `origin/main` から `hotfix/*` を作成し、`main` に入れた後で `develop` に取り込みます。
- 変更は小さく、目的を1つに絞るとレビューしやすくなります。

詳細は [docs/repository-governance.md](./docs/repository-governance.md) を参照してください。

## 4. 事前チェック

少なくとも以下を通してください。

```bash
./scripts/oss_preflight.sh
```

必要に応じて:

```bash
uv run python -m pytest -q
```

## 5. Pull Request

PR には次を含めてください。

- base branch と head branch が運用ルールに合っていること
- 変更の目的
- 何をどう変えたか
- 動作確認手順
- 既知の制約や未対応事項

`main` 向け PR は `release/v*`、`hotfix/*`、`dependabot/*` 以外を禁止します。

## 6. コーディング方針

- 既存の命名・構成・実装スタイルに合わせる
- 不要なリファクタは混ぜない
- 秘密情報・個人データ・大容量生データはコミットしない

## 7. Issue 運用

- 原則として **1 Issue = 1テーマ**（不具合1件、提案1件）
- 作成時はテンプレート（不具合報告 / 機能提案）を使用
- まず「何が問題か」「期待結果」を先に書く
- 不具合報告は再現手順と実行環境を必ず書く
- 軽い相談・メモは簡易Issue（空Issue）でも可
