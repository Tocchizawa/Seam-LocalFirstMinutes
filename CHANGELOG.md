# Changelog

All notable changes to Seam are documented here.
Format roughly follows [Keep a Changelog](https://keepachangelog.com/).

## [0.2.0-beta.1] - 2026-05-21

初の公開 beta リリース。

### Added

- 議事録一覧をカード型 3 行レイアウトに刷新 (日付 · 開始時刻 · 録音時間 / タイトル / 要約プレビュー)
- 一覧の検索バーを sticky 固定 + リストとの border 分離
- 要約タブでの検索ハイライト (tiptap Decoration ベース、アクティブマッチへ auto-scroll)
- 検索 prev/next が現在開いているタブのマッチに対して動作 (タブ切替時に findIndex リセット)
- 起動時セッション復元の進捗バナー (`RecoveryBanner`)。「未完了セッションを復元中 (3/5): 5/19 14:32 の会議」のように現在処理中のセッションが見える
- `GET /api/recording/recovery/status` エンドポイント (GUI 同期用)
- pyannote の segmentation / embedding / clustering 各ステージ進捗をフロントへ伝搬
- 手動 Release を一括実行する `scripts/release.sh`

### Changed

- DMG notarize/staple ワークフローを Apple 公式手順に合わせ刷新 (`Seam.app` と DMG の両方に staple ticket を付与、`Seam Installer` volname で macOS Sequoia の自己参照保護も回避)
- `create-dmg.sh` の DMG ファイル名を `tauri.conf.json` から動的導出

### Fixed

- 要約完了でタイトル自動生成されても一覧が更新されない (MainView が `summary_done` / `minutes_title_updated` 等を購読していなかった)
- summary_done 直後に詳細画面の要約本文が一瞬空になるフリッカー (`partial_text` を fallback で残すように)
- DetailView の reload 後に他コンポーネントが追従できるよう `minutes-updated` カスタムイベントを dispatch
