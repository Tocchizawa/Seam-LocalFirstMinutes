# データモデル

アプリデータは `~/.seam/` に置く。既存インストールも同じディレクトリを利用する。

## ファイル

| パス | 内容 |
| --- | --- |
| `config.yaml` | UI、録音、要約 provider などの設定 |
| `minutes.db` | 議事録、プロジェクト関連情報、SQLite FTS5 検索 index |
| `sessions/<session_id>/session_meta.json` | 録音中状態と異常終了後の復旧情報 |
| `sessions/<session_id>/combined.wav` | 録音中に追記する 16 kHz mono PCM |
| `sessions/<session_id>/combined.flac` | 録音停止後に確定する再生用音声 |
| `sessions/<session_id>/transcript.jsonl` | 確定した発話を一行ずつ記録する復旧用データ |

設定ファイルには Apple Speech の言語設定を保持する。旧 `whisper` 設定があっても文字起こし処理には使わない。

## 議事録

`minutes` テーブルには ID、session ID、project ID、タイトル、日付、開始時刻、録音秒数、transcript JSON、検索用 transcript text、要約、使用モデル名、作成・更新時刻を保持する。全文検索は title、summary、transcript text を SQLite FTS5 trigram index で検索する。

現行 transcript segment は次の項目を持つ。

```json
{
  "start": 12.3,
  "end": 15.8,
  "text": "次の議題に移ります。",
  "session_id": "20261119_103000"
}
```

speaker ID や話者名は新規データに付けない。既存議事録の JSON、SQLite の互換列、ユーザーの話者関連キャッシュは自動削除しない。現行 UI とエクスポートでは話者ラベルを使わない。

## 更新と互換性

SQLite はアプリ起動時に `CREATE TABLE IF NOT EXISTS` で初期化する。既存テーブルの列を削除する破壊的 migration は行わない。過去の議事録と録音ファイルを保持したまま、以降の新規録音を Apple Speech の形式で保存する。
