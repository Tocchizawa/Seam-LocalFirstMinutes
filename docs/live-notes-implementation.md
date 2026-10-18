# リアルタイム議論ノートの実装・検証記録

要件・不変条件の正本は [設計書](./SEAM_REALTIME_AGENTIC_NOTES_DESIGN.md)、
実行可能な起動・手動確認・復旧手順は [検証手順](./live-notes-testing.md) に記載する。
この記録ではモックによる確認と実機確認を区別する。

## 動作と設計判断

録音開始前にライブ方式・provider/model・安全なCLI設定を固定し、復旧用メタデータへ保存する。
Whisper確定発話には安定IDと順序を付け、セッションごとの直列処理でjournalへ保存しSQLiteへ取り込む。
DBが一時的に使えない場合はjournalを再取り込みし、初期化失敗も後続発話・停止・復旧時に再試行する。

適応バッチは未処理発話・全体目次・関連テーマをCLIへ渡し、専用の構造化JSON操作計画を受け取る。
通常1バッチ1推論、同時1ジョブ。入力・操作・時間・修復・呼び出し数に上限を設ける。
CLIは既存ログインを使用するが、録音後要約の権限や任意extra_argsを引き継がない。
必要な制限が使えないCLIはライブAIを利用不可とし、録音・手動編集は継続する。

アプリ側が型・根拠発話・所属session・revision・所有者を検証して、差分とcheckpointを原子的に保存する。
手動変更はhuman所有となりAI更新から保護され、競合案を保留する。
GUIは安定IDを持つブロック編集と永続ドラフトを使い、全文置換によるライブ更新を行わない。
WS欠番・再接続でsnapshotへ追いつき、IME入力・カーソル・選択・スクロールを維持する。

停止時は音声・最終発話・minutes保存を優先し、遅いCLI完了を待ち続けない。
topics/blocksを正本としてminutes.summaryへ投影する。旧自動要約・手動全文再要約・旧draft復元を防ぎ、
minutesへのリンクが確定するまで復旧メタデータを残す。再起動時は未完了バッチと欠けたリンクを復旧する。
現在の送信同意を取り消した場合は、会議開始時に同意していても後続送信を許可しない。

## Definition of Doneとの対応

| 設計書第17章の確認点 | 主な自動確認先 |
|---|---|
| 新話題・既存話題・過去話題への復帰、過去内容の保持 | store UT-01〜04、integrationの60分相当再生、GUIの目次・22論点再訪試験 |
| 種別・決定根拠・偽出典・不正操作の拒否 | store UT-05〜08/19と構造化入力の境界試験 |
| 手動追加・変更・削除・並べ替え・Undo、AI競合保護 | store UT-09〜11/15、GUIの競合・IME・ドラフト・履歴試験 |
| CLI遅延時も録音・発話・手動編集が継続 | integrationの遅延/pause、実録音callback/flush/finalize経路、GUI遅延表示試験 |
| Codex/Claude両CLI・制限引数・完了出力のみ反映 | providersのsubprocessスタブ契約試験、設定GUI試験 |
| 冪等・履歴・checkpoint・rollback・呼び出し上限 | store UT-11〜14/20、integrationの永続呼び出し予算試験 |
| WS・クラッシュ・初期化失敗からの復旧 | integrationのjournal/processing復旧、lifecycle-reviewの初期化再試行・順序・リンク復旧、GUI再同期試験 |
| 停止後minutesへの関連・編集・検索・Markdown | integrationのlink/search/exportと実callback/finalize、GUI停止・従来議事録試験 |
| 既存全文要約が上書きしない、OFF時の回帰防止 | integrationの旧summary拒否、既存Python/script/API回帰、GUI legacy試験 |
| Python・GUIビルド・既存preflight | 下記コマンド。実行結果と環境依存の未実施は分けて記録 |

GUI試験はブラウザー内のAPI/WS fixtureで操作を再現する。バックエンドの60分相当試験は
実際のSQLite・router・coordinatorに合成発話とCLIスタンドインを接続する。
これらを、実音声60分録音やクラウドモデルの意味的品質評価と同一視しない。

## 実行コマンドと確認範囲

```bash
uv sync
uv run python -m pytest tests/ -q -rs
uv run python scripts/run_tests.py --scripts
uv run python scripts/run_tests.py --api
pnpm -C gui install --frozen-lockfile
pnpm -C gui exec playwright test
pnpm -C gui build
./scripts/oss_preflight.sh
```

2026-10-17の検証では、旧直接実行入口19スクリプト・内部283チェックと、
隔離バックエンドに対するPython API126チェック・shell API44チェックが成功した。
最終の全pytestは219項目成功・失敗0・環境依存5項目skip（49.44秒）、旧トップレベルスクリプト内部206チェック成功だった。
実行コマンドは `.venv/bin/python -m pytest tests/ -q -rs --tb=short`。
GUIは `pnpm -C gui test:e2e` で19シナリオ成功・失敗0（19.6秒）、TypeScript/Viteビルドも成功した。
この環境では既存Chromeを `PLAYWRIGHT_CHROMIUM_EXECUTABLE` で指定した。
最後の状態通知調整後にもscheduler/integrationの17項目を再実行し、全件成功した。
新規Pythonの静的検査（ruffのF/I/UP）、`git diff --check` も成功した。
GUIを含むOSS preflightは成功し、Viteの大きなbundle chunkに関する警告が残った。

pytest項目数、script件数、script内部チェック数は異なる単位であり合算しない。
API試験はpytest上の手動前提skipとは別に `--api` で実行済み。
MLXがサンドボックス終了時にMetalデバイス不在の警告を出す場合がある。実Whisper推論の成功を意味しない。

## 追加・変更ファイルの担当範囲

| 分類 | ファイル |
|---|---|
| ライブノート本体 | `src/live_notes/{models,repository,coordinator,prompts,renderer}.py`、`src/live_notes/providers/`、パッケージ初期化 |
| API・録音・保存・旧要約保護 | `src/api/{live_notes,recording,minutes,settings,summarize}.py`、`src/{config,main}.py`、`src/storage/db.py`、`src/summarize/runner.py`、`src/transcribe/streaming.py` |
| 既存不具合の修正 | `src/audio/recorder.py`：同梱ffmpegからMP3時間を取得し、既存converter試験の失敗を修正 |
| GUI | `gui/src/lib/live-notes.ts`、`gui/src/components/live-notes/`、`gui/src/pages/{LiveView,DetailView,MainView,Settings}.tsx` |
| 自動試験・依存 | `tests/test_live_notes_{store,providers,integration,lifecycle_review,scheduler}.py`、`tests/conftest.py`、`tests/test_api.{py,sh}`、`tests/test_recording_pipeline_failures.py`、`pytest.ini`、`scripts/run_tests.py`、`pyproject.toml`、`uv.lock` |
| GUI試験 | `gui/tests/`、`gui/playwright.config.ts`、`gui/package.json`、`gui/pnpm-lock.yaml`、GUI/rootのignore設定 |
| ドキュメント | README、architecture、data-model、requirements、summarize-design、専用設計書、検証手順、本記録 |

## 未実施・制約

- 実マイク・ScreenCaptureKit・実Whisper・話者分離による実機録音試験は別途必要。
- 音声fixtureと旧faster-whisperモデルがないため、実音声/Ollamaの既存精度評価は未実施。
- Codexは後述の合成会話1バッチで実クラウド推論とノート保存を確認した。Claudeの実推論・実会議での精度比較は未実施。スタブ契約試験の成功を本番CLI・認証・利用枠の保証としない。
- ユーザー設定のCLIバイナリ・ランチャー自体は信頼対象。モデルのツールを無効化する構成で、新しいOSレベルの隔離は追加していない。
- CLIサービス側やバージョンの変化で必要な制限機能が使えない場合は、明示的に利用不可となる。
- 根拠参照・決定語検証は捏造や誤分類を完全には排除しない。重要な決定・担当者・期限は人が確認する。
- 秒単位の応答、同時共同編集、セクション横断の一括リッチテキスト編集は保証しない。
- bundle分割、履歴保存量の最適化、実モデルでのp50/p95測定、長会議での関連テーマ検索精度は改善候補。

関連テーマの入力は上位5論点・各12ブロック以内、本文抜粋は各2000字・合計36000字以内に制限する。
SQLiteの手動本文・元発話は短縮せず保存し、表示・出力には保存済み全文を使う。
応答時間のp50/p95は保存したCLI呼び出しからsnapshotのmetricsへ集計するが、今回の値はスタブの時間であり実サービスの性能測定ではない。

## 変更ファイル一覧（完全）

```text
.gitignore
README.md
docs/SEAM_REALTIME_AGENTIC_NOTES_DESIGN.md
docs/architecture.md
docs/data-model.md
docs/live-notes-implementation.md
docs/live-notes-testing.md
docs/requirements.md
docs/summarize-design.md
gui/.gitignore
gui/package.json
gui/playwright.config.ts
gui/pnpm-lock.yaml
gui/src/components/live-notes/LiveNoteBlockEditor.tsx
gui/src/components/live-notes/LiveNotesPanel.tsx
gui/src/components/live-notes/LiveNotesRecovery.tsx
gui/src/components/live-notes/LiveNotesSettings.tsx
gui/src/components/live-notes/live-notes.css
gui/src/lib/live-notes.ts
gui/src/pages/DetailView.tsx
gui/src/pages/LiveView.tsx
gui/src/pages/MainView.tsx
gui/src/pages/Settings.tsx
gui/tests/live-notes-harness.html
gui/tests/live-notes-harness.tsx
gui/tests/live-notes.spec.ts
pyproject.toml
pytest.ini
scripts/run_tests.py
src/api/live_notes.py
src/api/minutes.py
src/api/recording.py
src/api/settings.py
src/api/summarize.py
src/audio/recorder.py
src/config.py
src/live_notes/__init__.py
src/live_notes/coordinator.py
src/live_notes/models.py
src/live_notes/prompts.py
src/live_notes/providers/__init__.py
src/live_notes/providers/base.py
src/live_notes/providers/cli.py
src/live_notes/providers/process.py
src/live_notes/renderer.py
src/live_notes/repository.py
src/main.py
src/storage/db.py
src/summarize/runner.py
src/transcribe/streaming.py
tests/conftest.py
tests/test_api.py
tests/test_api.sh
tests/test_live_notes_integration.py
tests/test_live_notes_lifecycle_review.py
tests/test_live_notes_providers.py
tests/test_live_notes_scheduler.py
tests/test_live_notes_store.py
tests/test_recording_pipeline_failures.py
uv.lock
```

## ライブ専用モデル選択（2026-10-18）

`live_notes.models.codex` / `live_notes.models.claude_code` にprovider別のモデルIDを保存する。
空文字なら既存の要約設定を継承する。CLI候補一覧からの選択と、キャッシュ不在時の直接入力を用意した。
Codexの候補にGPT-6 Lunaがある場合は「軽量・ライブ推奨」を表示する。
これは頻繁な更新向けの選択案であり、実会議に対するモデル間の速度・品質比較は未実施。
[OpenAIのモデル説明](https://developers.openai.com/api/docs/models/gpt-6-luna)を参照。

`resolve_live_provider()` を録音開始時のmetadata固定・coordinator初期化・推論なしpreflightで共用し、
解決したCLIモデルをSQLiteに保存する。録音中の設定変更は次の会議だけに適用し、再起動時も元のモデルを維持する。
要約設定のモデルやCLI認証は変更しない。

主な変更箇所は `src/live_notes/models.py`、`coordinator.py`、`src/api/{recording,live_notes,settings}.py`、
`gui/src/components/live-notes/LiveNotesSettings.tsx`、`live-notes.css`、`gui/src/lib/live-notes.ts`、
`gui/src/pages/Settings.tsx`、`tests/test_live_notes_models.py`、GUI試験fixture/spec、README、architecture、検証手順。

検証：Python全体 `python -m pytest tests/ -q --tb=short` は226成功・5skip（旧スクリプト内部207成功）。
関連試験の先行実行は112成功。GUI E2Eは21成功（モデル選択・provider別保持・直接入力・継承復帰・表示検証を含む）。
GUI/Tauri Releaseビルド、OSS preflight、変更箇所のRuff F/I/UP、`git diff --check` は成功。
Chromeは実行環境の起動制約を解除して再実行し、デスクトップ1440×900/モバイル390×844の画像を確認した。
このモデル選択追加時点では実マイク・実モデル推論は追加実行していなかった。後続の実Codex確認は以下に記録する。既存のbundleサイズ警告とsandbox環境でのMLX終了時Metal警告は残る。

## Codex実行互換性と実推論確認（2026-10-18）

ユーザー報告のCLIエラーをローカルで再現した。Codex 0.163.0-alpha.10.2は
`--strict-config` と `tools.view_image=false` の組み合わせを未知の設定として拒否した。
旧設定キーを除去し、検出した `view_image` featureを明示的に無効化する。
画像ツール制限のfeatureを検出・検証できなければ利用不可とする。

preflightは従来のhelp/features確認に加え、実際のexec引数をローカルで検証する。
存在しないschemaファイルでモデル呼出し前に停止することを確認し、空の認証ディレクトリと
loopback専用providerも指定してクラウド送信を防ぐ。未知の設定をhelp確認だけで見逃さない。

エラーは安全な固定メッセージへ分類し、CLI設定・認証・利用枠・モデル・出力Schema・接続・
ローカル実行環境を区別して表示する。生のstdout/stderrや発話本文は保存・表示しない。
モデル向けJSON Schemaには操作別の必須項目/null項目も明示し、アプリ検証との不一致を防ぐ。
形式違反は入力を含まないフィールドパスと種別だけで診断し、修復指示へ渡す。

明示許可を得て合成会話だけでCodexを実行した。最初の起動試験はsandbox内のローカルDB/IPC制限で
送信前に失敗。通常権限での初回モデル応答はアプリの操作形式検証で停止した。
Schema強化後の追加試験は、gpt-6-luna / low / 呼出し1回 / 修復0回で成功した。
合成発話2件が処理済みseq=2・未整理0件となり、「認証方式」の見出しとdecision/action/questionを
一時SQLiteへ保存した。実会議のデータは送信していない。追加許可2回のうち1回だけ使用した。
成功応答は `tests/fixtures/live_notes_codex_luna_plan.json` に合成データの回帰fixtureとして保存した。

修正後の最終検証：`python -m pytest tests/ -q --tb=short` は243成功・5skip・失敗0、
旧スクリプト内部207成功。Ruff F/I/UP・OSS preflight・`git diff --check` とGUI/Tauri Releaseビルドは成功。
実Codexのローカルexec互換性probeもpytestに含めた（CLI未導入環境ではこの1件をskip）。
実推論の成功応答を再利用したテストはクラウドへ送信せず、SchemaとSQLiteへの差分適用を検証する。
GUIコードは今回変更せず、直前の21件E2E確認済みのものを同梱する。
配布物は `gui/src-tauri/target/release/bundle/zip/Seam_0.2.3_live-cli-fix_aarch64.zip`。
