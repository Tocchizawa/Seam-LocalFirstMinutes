# Contributing Guide

このプロジェクトへの貢献ありがとうございます。変更前に以下を確認してください。

## 言語運用

- 日常運用（Issue / PR / レビューコメント）: 日本語
- 公開向け PR タイトルとリリースノート: 英語
- ドキュメント: 日本語

コミットメッセージは既存の形式に合わせ、変更内容を短く記述してください。

## 開発環境

必要なものは Apple Silicon Mac、macOS 26 以降、Xcode Command Line Tools、Python 3.11 以降、`uv`、Swift 6.2 以降です。

```bash
uv sync
uv run python -m src.main
```

ネイティブアプリのビルド:

```bash
swift build --package-path macos --product SeamApp
swift build --package-path macos --product AppleSpeechTranscriber
```

## 変更方針

- 既存の命名・構成・実装スタイルに合わせる
- 依頼された範囲に絞り、必要性の低い依存追加や整理を避ける
- 秘密情報、個人データ、大容量の生データをコミットしない
- PR には変更目的、実施内容、確認方法、既知の制約を記載する
