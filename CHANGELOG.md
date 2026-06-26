# Changelog

All notable changes to Seam are documented here.
Format roughly follows [Keep a Changelog](https://keepachangelog.com/).

## [0.2.0-beta.2] - 2026-06-23

録音の安定性と配布版アップデート時の復旧性を改善する beta リリース。

### Fixed

- 長時間録音でマイク音声とシステム音声が途中から小さくなるケースに対し、録音中の文字起こし入力へRMSベースの自動ゲイン補正を追加
- 録音停止後に生成する `combined.flac` へ `amix normalize=0` と `dynaudnorm + alimiter` を適用し、保存音声の音量低下を補正
- macOSでアプリをしばらく放置した後などにPortAudioの一時的な内部エラーでマイク録音開始に失敗するケースを、音声バックエンドのreset/retryで回復
- DB保存に失敗した録音を成功扱いにせず、復旧可能なセッション情報を残したまま `pipeline_error` として通知
- 録音中にマイクキャプチャが落ちた場合に、バックエンドとフロントエンドの録音中表示が残り続けないように状態同期を修正
- システム音声の開始/変換失敗を明示的に失敗扱いにし、要求されたシステム音声が取れなかった録音を成功したマイク単独議事録として保存しないように修正
- 配布版アップデート時に同梱バックエンドの古いPythonファイルが `python-env` に残らないよう、temp展開とrollback可能な差し替えに変更

### Changed

- 音声レベリング仕様に合わせて要件定義・アーキテクチャ・データモデルを更新

### Tests

- 音量低下補正、録音前preflight、録音パイプライン失敗、システム音声失敗の回帰テストを追加
- 対象PR: [#38](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/38), [#46](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/46)

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
