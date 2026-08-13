# Changelog

All notable changes to Seam are documented here.
Format roughly follows [Keep a Changelog](https://keepachangelog.com/).

## [0.2.1] - 2026-08-13

This release stabilizes recording startup and keeps the Whisper model controls compact.

### Changed

- Keep the settings model list compact and remove duplicate download status from the recording toolbar.

### Fixed

- Keep the Whisper resource lease until recording cleanup when a streaming worker is restarted unexpectedly.
- Prevent recording startup from getting stuck while the Whisper worker is being recovered.

### Tests

- `uv run python -m unittest tests.test_streaming_generation tests.test_resource_monitor`
- `pnpm build`
- Signed and notarized macOS `.app` / DMG build

## [0.2.0-beta.16] - 2026-08-07

This beta improves Whisper model management and makes model preparation visible during recording start.

### Changed

- Replace the separate Whisper model selector with a compact selectable model list.
- Show download spinner, percentage, and speed in the settings and recording views.

### Fixed

- Reuse valid cached Whisper snapshots even when the Hugging Face `refs/main` file is missing.
- Keep the model download status visible while recording startup is still in progress.
- Avoid showing a fresh download state when the selected model is already cached.

### Tests

- `uv run python -m unittest tests.test_model_management tests.test_streaming_model_loader tests.test_model_loader`
- `pnpm --dir gui build`
- `python3 -m py_compile src/transcribe/streaming.py`

## [0.2.0-beta.15] - 2026-08-06

This beta fixes downloading and loading the `large-v3-turbo` Whisper model.

### Fixed

- Accept Hugging Face Whisper snapshots that use `weights.safetensors` as well as `weights.npz`.
- Report both supported weight formats when a model snapshot is invalid.

### Tests

- `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_model_management tests.test_model_loader tests.test_streaming_model_loader`
- `bash scripts/oss_preflight.sh`
- Tauri smoke build without bundling

## [0.2.0-beta.14] - 2026-08-06

This beta adds on-demand Whisper model management and download progress.

### Added

- Add settings controls to download and delete supported Whisper models.
- Show byte-level model download progress in settings and while recording starts.
- Add model catalog, download, and delete APIs.

### Changed

- Share the Hugging Face cache and download state between settings and recording.
- Remove the unused log-based aggregate download-progress parser.

### Tests

- `./.venv/bin/python -m unittest tests.test_model_management tests.test_model_loader tests.test_streaming_model_loader`
- `pnpm build`
- Tauri smoke build without bundling

## [0.2.0-beta.13] - 2026-08-06

This beta reduces recording storage and local AI resource contention, and gives Codex CLI connection checks enough startup time on macOS.

### Added

- Add CPU, memory, process-priority, and local-model coordination controls for Whisper and summary processing.
- Add a sticky settings save footer with unsaved-change confirmation.
- Add runtime resource snapshots for diagnostics.

### Changed

- Save new mixed recordings as 16 kHz mono FLAC and stop generating a redundant playback WAV.
- Prefer canonical FLAC for playback and recovery while retaining legacy WAV compatibility.
- Load slower settings data only when its category is opened.
- Increase the Codex CLI connection-test timeout from 12 to 30 seconds and migrate the previous default.

### Tests

- `./scripts/oss_preflight.sh`
- `PYTHONPATH=. ./.venv/bin/python tests/test_summarize_foundation.py`
- `PYTHONPATH=. ./.venv/bin/python tests/test_cli_providers.py`
- `PYTHONPATH=. ./.venv/bin/python tests/test_recording_pipeline_failures.py`
- `PYTHONPATH=. ./.venv/bin/python tests/test_resource_monitor.py`
- Signed and notarized macOS `.app` / DMG build

## [0.2.0-beta.12] - 2026-08-06

This beta improves recording controls and keeps ScreenCaptureKit internal audio capture recoverable across display-idle sleep and OS-initiated stream stops.

### Added

- Add settings for speaker diarization, custom summary prompts, transcription throttling, quit confirmation, and native completion notifications.
- Add structured ScreenCaptureKit diagnostics for stream errors, captured bytes, elapsed duration, restart reasons, and recovery state.

### Fixed

- Prevent display-idle sleep while internal audio recording is active.
- Recreate the same ScreenCaptureKit capture path after stream stops, sidecar exits, raw-byte stalls, or a silent tail after valid audio.
- Preserve every captured RAW segment and insert silence only for the measured recovery gap.
- Wait for the display capture source to return without consuming the recovery budget, while limiting consecutive non-display restart failures.
- Avoid repeated silent-tail restarts until the recreated stream has captured new non-silent audio.
- Use `Seam Setup` as the DMG volume name to avoid macOS mount-path conflicts.

### Tests

- `./scripts/oss_preflight.sh`
- `uv run python tests/test_system_capture_failures.py` (27 tests)
- `uv run python tests/test_recorder_preflight.py` (11 tests)
- `uv run python tests/test_recording_pipeline_failures.py` (6 tests)
- `PYTHONPATH=. uv run python tests/test_audio_leveling.py` (3 tests)
- `uv run python tests/test_streaming_generation.py` (3 tests)
- `cargo test --manifest-path gui/src-tauri/Cargo.toml -- --nocapture` (3 tests)
- Objective-C sidecar compile with `-Wall -Wextra -Werror`
- Target PR: [#82](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/82)

## [0.2.0-beta.11] - 2026-07-13

This beta release replaces the internal system audio capture path with ScreenCaptureKit and removes the previous Core Audio Tap fallback paths.

### Fixed

- Use ScreenCaptureKit as the only internal system audio capture backend.
- Fail recording startup when requested internal audio cannot start, instead of continuing as a microphone-only recording.
- Detect recordings where ScreenCaptureKit writes raw bytes for the full duration but produces only silence.
- Reject removed backend modes such as `coreaudio_tap`, `tap`, and `blackhole`.

### Changed

- Update the native `audio-capture` sidecar to write ScreenCaptureKit audio as mono `f32le` raw PCM with metadata.
- Update internal audio documentation and availability checks to describe ScreenCaptureKit-only capture.
- Bundle the native sidecar with ScreenCaptureKit, CoreMedia, CoreGraphics, AppKit, CoreAudio, and AudioToolbox frameworks.

### Tests

- `uv run python tests/test_system_capture_failures.py`
- `uv run python tests/test_recorder_preflight.py`
- `uv run python tests/test_recording_pipeline_failures.py`
- `PYTHONPATH=. uv run python tests/test_audio_leveling.py`
- `uv run python tests/test_audio_devices.py`
- `uv run python -m py_compile src/api/recording.py src/audio/recorder.py src/audio/system_capture.py src/config.py src/api/devices.py tests/test_system_capture_failures.py tests/test_recorder_preflight.py tests/test_recording_pipeline_failures.py tests/test_audio_leveling.py tests/test_audio_devices.py`
- `xcrun clang ... sidecar/audio-capture/Sources/main.m -o /tmp/seam-audio-capture-test`
- `cargo check --manifest-path gui/src-tauri/Cargo.toml`
- `pnpm --dir gui build`
- `pnpm build`
- Target PR: [#75](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/75)

## [0.2.0-beta.10] - 2026-07-09

スプラッシュ画面の最初に起動時アップデート確認を挟み、自動アップデートを許可している場合は通常起動前に更新を適用する beta リリース。

### Fixed

- 起動時アップデート確認がバックエンド起動後に走っていたため、最初の読み込み画面で更新処理が見えない問題を修正
- `auto_install_on_startup=true` の場合、スプラッシュ画面上で更新確認、ダウンロード、インストール、再起動まで進むように修正

### Changed

- 起動時アップデート確認または自動更新の継続判断が終わるまで、バックエンド起動と通常 UI 初期化を待機するように変更
- 起動直後に backend API を呼ばずに `~/.seam/config.yaml` の `app_update` 設定を Tauri 側で読み取るように変更
- 更新確認のみ有効な場合は、通常起動後に設定画面へ誘導する通知を表示するように変更
- 自動更新失敗時はスプラッシュ上で失敗理由を短く表示し、その後通常起動へ進むように変更

### Tests

- `cargo test --manifest-path gui/src-tauri/Cargo.toml -- --nocapture`
- `pnpm --dir gui build`
- `git diff --check`
- `pnpm dev` で、更新ゲート後にバックエンドが起動し、`/health` と通常 API が 200 になることを確認
- 対象PR: [#71](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/71)

## [0.2.0-beta.9] - 2026-07-09

Codex CLI / Claude Code CLI などの CLI 型要約プロバイダで、接続不可や認証不備を早く検知できるようにした beta リリース。

### Fixed

- 手動要約の開始前に CLI 型プロバイダの preflight を行い、CLI 未導入、未認証、ネットワーク不可、プロバイダ障害、タイムアウトを早期にエラー表示するように修正
- Codex CLI / Claude Code CLI の実行前にシェルのコマンドキャッシュを消去し、ログインシェル由来の PATH を読み直してから実行するように修正
- Codex CLI の設定保存で `minutes_ai.codex` 配下の既存設定が失われないように修正

### Changed

- CLI 型プロバイダの接続失敗を `AUTH_FAILED` / `OFFLINE` / `TIMEOUT` / `PROVIDER_DOWN` などの扱いやすい分類へ正規化
- 手動要約APIの前処理でプロバイダ状態を確認し、失敗時は長い要約処理へ進まず即時に応答するように変更

### Tests

- `uv run python tests/test_summarize_foundation.py tests/test_settings_api.py`
- `pnpm --dir gui build`
- 対象PR: [#68](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/68)

## [0.2.0-beta.8] - 2026-07-09

アプリ起動時と設定画面から更新できる、署名検証付きのアプリ内アップデートを追加する beta リリース。

### Added

- Tauri updater によるアプリ内アップデート機能を追加
- 設定画面に「アプリ」カテゴリを追加し、現在バージョン、起動時の更新確認、起動時自動アップデート、手動更新を操作できるように変更
- 起動時は設定に従って更新確認し、ユーザーが `起動時に自動アップデート` を有効にした場合のみ自動インストールと再起動まで進めるように変更
- GitHub Releases に `Seam.app.tar.gz` / `.sig` / `latest.json` を publish し、固定フィード `updater-feed/latest.json` を更新する release workflow を追加

### Changed

- 録音中は起動時・手動更新ともインストールや再起動に進まず、録音終了後に更新する案内に留めるように変更
- Tauri capability を updater の check/download/install と process restart に限定
- release 用 updater artifact は署名・公証済みの最終 `.app` から作成し、配布時は codesign / notarization 検証を必須化

### Tests

- `cargo check --manifest-path gui/src-tauri/Cargo.toml`
- `pnpm --dir gui build`
- `pnpm --dir gui tauri build --bundles app --ci`
- `uv run python tests/test_summarize_foundation.py`
- backend 一時起動で `app_update` の既定値と保存時正規化を確認
- updater artifact / signature / feed JSON の生成を確認
- 対象PR: [#66](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/66)

## [0.2.0-beta.7] - 2026-07-02

マイク入力を Core Audio sidecar に寄せ、PortAudio の mic overflow 起因で自分の音声が途切れるケースを避ける beta リリース。

### Fixed

- マイク録音の既定 backend を Python/sounddevice の blocking read から Core Audio Audio Queue sidecar に変更
- mic sidecar の raw PCM を Python 側で追尾し、`mic.wav` とリアルタイム文字起こし入力へ流す構成に変更
- 配布ビルドの audio-capture sidecar に microphone mode を同梱

### Tests

- Core Audio mic sidecar 単体起動、`mic.wav` 生成、`Recorder.start()` / `Recorder.stop()` 経由の `combined.flac` 生成をローカル確認
- 録音 preflight / 録音パイプライン失敗 / audio leveling / デバイス refresh の回帰テストを実行
- 対象PR: [#63](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/63)

## [0.2.0-beta.6] - 2026-07-02

マイク入力の overflow 時に、推定無音を録音へ差し込まないようにして、マイク音声だけが断続的に壊れるケースを抑止する beta リリース。

### Fixed

- PortAudio の mic buffer overflow 時に、欠落時間を推定して mic WAV / live mixer へ無音 padding を挿入していた処理を削除
- overflow 連続時にマイクストリームを再オープンし、block size を自動変更する処理を削除

### Changed

- `recording.mic_stream` から overflow 起因の再オープン設定を削除し、マイク音声は実際に読み取れたサンプルだけを保存する方針に整理

### Tests

- 録音 preflight / 録音パイプライン失敗 / audio leveling の回帰テストを実行
- 対象PR: [#61](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/61)

## [0.2.0-beta.5] - 2026-07-02

録音完了後の文字起こし確定フローを整理し、録音中に確定した transcript をそのまま保存する beta リリース。

### Fixed

- 録音完了後に自動で再文字起こしが始まり、既存の文字起こし結果が表示されないケースを修正
- 詳細画面で空の transcript が既存のライブ文字起こし表示を上書きしないように修正

### Changed

- 録音完了時の一覧ステータスを「再文字起こし中」固定ではなく、通常の「文字起こし中」として表示
- 録音停止後の保存用 transcript を `combined.flac` から自動再生成する設計記述を廃止し、手動再文字起こし時のみ `combined.flac` を使う方針に更新

### Tests

- combined 音声が存在しても録音完了時に自動再文字起こしを起動しない回帰テストを追加
- 対象PR: [#58](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/58), [#59](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/59)

## [0.2.0-beta.4] - 2026-06-30

Core Audio Tap を唯一の内部音声キャプチャ経路にし、ScreenCaptureKit / BlackHole フォールバックを廃止する beta リリース。

### Changed

- システム音声キャプチャを Core Audio Process Tap sidecar のみに統一
- Core Audio Tap が使えない場合は内部音声録音を開始失敗として扱うように変更
- 内部音声の利用可否 API に `system_audio_available` を追加し、既存 UI 互換用の `screen_capture_available` は維持
- ScreenCaptureKit / BlackHole フォールバック前提の要件定義・アーキテクチャ・技術スタック・データモデルを更新
- 配布ビルドの Info.plist から `NSScreenCaptureUsageDescription` を削除し、システム音声取得権限の説明へ整理

### Tests

- Core Audio Tap sidecar 起動失敗・変換失敗時の回帰テストを更新
- 録音パイプライン失敗テストの内部音声エラー文言を Core Audio Tap 前提に更新
- 対象PR: [#56](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/56)

## [0.2.0-beta.3] - 2026-06-30

Core Audio Tap ベースの内部音声キャプチャ、起動時の復旧表示、録音デバイス更新まわりを改善する beta リリース。

### Added

- macOS 14.2+ 向けに Core Audio Process Tap sidecar を追加し、内部音声キャプチャの第一候補として利用
- Splash 画面でバックエンド起動失敗や復旧状況を確認できる表示を追加
- 議事録一覧の初期表示後に追加読み込みできるページング導線を追加
- デバイス設定でマイクデバイス一覧を再読み込みできる導線を追加

### Fixed

- Whisper モデルロードが詰まった場合に stale loader lease を timeout で復旧できるように修正
- 古い世代の streaming transcribe 結果が現在の録音状態へ反映されるケースを抑止
- アプリ起動後に追加されたマイクがデバイス一覧へ反映されないケースを、PortAudio の再初期化で更新
- `sounddevice` の既定入力デバイス判定を `_InputOutputPair` に対応

### Changed

- Core Audio Tap を前提に、要件定義・アーキテクチャ・技術スタック・データモデルを更新
- 配布ビルド時に audio-capture sidecar を Tauri resources へ同梱するように更新

### Tests

- Core Audio Tap / system capture 失敗時の回帰テストを追加
- streaming transcribe の世代管理とモデル loader timeout の回帰テストを追加
- マイクデバイス一覧 refresh の回帰テストを追加
- 対象PR: [#47](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/47), [#49](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/49), [#50](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/50), [#51](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/51), [#53](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/53), [#54](https://github.com/Tocchizawa/Seam-LocalFirstMinutes/pull/54)

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
