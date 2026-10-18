# ライブノートの検証・復旧

仕様と完了条件は [SEAM_REALTIME_AGENTIC_NOTES_DESIGN.md](./SEAM_REALTIME_AGENTIC_NOTES_DESIGN.md)
の第16・17章を参照する。この文書は実行手順であり、実機確認済みを意味しない。

## 自動試験

macOS / Python 3.11+ / uv / Node.js 20+ / pnpmを用意して実行する。

```bash
uv sync
pnpm -C gui install --frozen-lockfile
uv run python -m pytest tests/ -q -rs
uv run python scripts/run_tests.py --scripts
uv run python scripts/run_tests.py --api
pnpm -C gui build
./scripts/oss_preflight.sh
```

pytestは通常のテスト関数・unittestと、トップレベルで実行して終了する旧チェックを扱う。
旧チェックはファイルごとにsubprocessで実行するため、他のテストを終了・汚染しない。
pytestの合格数は「テスト項目数」であり、別記する旧スクリプト内PASSチェック件数と混同しない。
`scripts/run_tests.py --scripts` は既存の直接実行入口もファイル単位で検証し、両方の件数を出す。
各自動試験には使い捨てHOMEを割り当て、通常の `~/.seam` を使わない。
CLIスタブ試験は実モデル推論・クラウド送信を行わない。
`--api` は空いたローカルポートに一時HOMEのバックエンドを起動し、既存のPython・shell API試験を実行する。
この経路ではOllama自動起動・Whisper preload・録音後自動要約を無効にし、試験終了時にサーバーを停止する。

GUIの自動E2Eは `pnpm -C gui exec playwright test` で実行する。
初回は `pnpm -C gui exec playwright install chromium` でブラウザーを用意する。
既存のChromiumを使う場合は `PLAYWRIGHT_CHROMIUM_EXECUTABLE` に実行ファイルを指定できる。
Tauri・macOS権限・本物のマイク・クラウドの意味的精度は、下記の実機試験と区別する。

## 外部環境が必要な既存試験

モデル選択の回帰試験は `uv run python -m pytest tests/test_live_notes_models.py -q`。
設定APIでのprovider別モデル保存、要約設定との分離、未指定時の継承、会議開始時の固定、
再起動後の固定モデル復元を検証する。GUI E2Eには候補選択・provider切替・
キャッシュ不在時の直接入力・継承への復帰・デスクトップ/モバイル表示を含む。
`live_notes.models.codex` / `live_notes.models.claude_code` が空文字なら要約設定を継承する。
モデル候補・制限機能の確認は、アカウントでの推論成功を保証しない。

以下は自動試験から削除せず、pytestで前提条件付きskipとして報告する。
`--run-manual` ですべてを無条件実行するより、各試験を個別に準備して実行する。
通常利用中のSeamやDBへ接続しないよう、専用のテスト用HOMEを作成する。

| 既存試験 | 前提条件 |
|---|---|
| `test_api.py` / `test_api.sh` | 専用HOMEで起動したlocalhost:18900バックエンド。DB直接書込試験とサーバーのHOMEを一致させる |
| `test_pipeline.py` | `/tmp/seam-audio-test/tts_meeting.wav` の日本語TTS会議音声、別途 `faster-whisper`、モデルダウンロード、十分なCPU時間 |
| `test_pipeline_real.py` | `/tmp/seam-audio-test/meeting.wav` の約610秒日本語対談、`faster-whisper` medium（CPU/int8）、十分なCPU時間 |
| `test_summarize.py` | 上記音声、`faster-whisper`、起動済みOllamaと `qwen3:8b`。ローカルモデルで実推論する |
| `test_summarize_integration.py` | 最低1件のminutesを入れた専用HOME。Ollama・インストール済みCLIの検出を試みるため利用環境を確認する |

音声の品質評価用旧スクリプトは `faster-whisper` を利用するが、現在のアプリの文字起こしは
`mlx-whisper`。音声fixtureと追加モデルは通常依存に含めず、実機試験を行う環境へ別途準備する。
録音試験ではマイク・画面録音権限、ScreenCaptureKit、ffmpeg、必要に応じたHuggingFace認証も確認する。

例（fixtureを準備済みの専用HOMEで実行）:

```bash
uv run python scripts/run_tests.py --manual test_pipeline.py --home /tmp/seam-manual-home
```

実Codex/Claudeの推論試験は、明示的な許可・ログイン・利用枠を確認して合成会話だけで行う。
CLIが存在する、helpが表示できる、スタブ契約試験が成功する、という結果を実推論成功と報告しない。

## 手動E2Eの受け入れ手順

1. 専用プロジェクトを作り、ライブノートをOFFで短く録音する。従来要約・編集・検索・Markdown出力を確認する。
2. CLIを選択し、送信説明に同意してライブノートをONにする。録音画面のノート・文字起こし・履歴を確認する。
3. 「認証方式」「API仕様」の順に発話し、再び認証方式へ戻る。元の見出しへ蓄積され、過去内容が残ることを確認する。
4. 曖昧な提案と明確な合意を別々に発話し、提案と決定が区別され、根拠発話が保持されることを確認する。
5. 過去のブロックを日本語IMEで編集中、別ブロックをAI更新する。カーソル・選択・スクロール・入力を保持する。
6. 同じブロックのAI更新と保存を競合させる。手動文が残り、競合案・409・再試行で未保存入力を破棄しない。
7. 手動で見出し・メモを追加し、並べ替え、削除確認とUndoを行う。AI一時停止中も発話と編集が保存される。
8. WSを切断・再接続する。snapshotから追いつき、重複表示・古い版への巻き戻りがない。
9. CLIを遅延・失敗させ、録音・文字起こし・手動編集が継続することを確認する。利用上限と再試行状態も確認する。
10. 録音停止直後に詳細を開き、同じノートが残ること、未処理分を明示すること、旧要約で上書きしないことを確認する。
11. Markdown出力と全文検索を確認して再起動する。未完了処理・保留案・手動変更・履歴を復元できることを確認する。

60分相当・20論点以上の再生試験では、最後に全論点への再訪を含める。
手動編集消失0件、seq欠落・二重適用0件、呼び出し上限遵守を検証し、所要時間と件数を記録する。
実時間60分の録音、加速したモック再生、実モデル推論はそれぞれ別の検証結果として報告する。

既存APIの自動化確認では `scripts/run_tests.py --api` によりPython側126件・shell側44件の
既存チェックを実行する。チェック件数は2026-10-17時点の内訳で、pytestの項目数とは別に集計する。

## 障害時の操作と制約

- AIエラー時も録音・手動編集を続ける。認証や利用枠を解消して「再試行」し、未処理発話を順に整理する。
- アプリ再起動後は保存済みノートを開く。音声を復元できない場合もノートの閲覧・編集は可能。
- 編集保存が失敗した場合は、入力を保持したまま再試行・競合解決する。保存済みと表示されるまでドラフトを手動削除しない。
- バックアップは通常終了後の `~/.seam` 全体を対象とする。稼働中DBの単独コピーはWALを取り逃す可能性がある。
- ノートは発話認識とLLMの誤りを含み得る。決定・担当・期限を会議参加者が確認し、必要に応じて手動修正する。
- セクションをまたぐ一括リッチテキスト編集や共同編集は対象外。全体の持ち出しはMarkdown出力を使う。
