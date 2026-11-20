# 要件

## 対象環境

- Apple Silicon の macOS アプリ
- macOS 26 以降（Apple Speech のオンデバイス認識を利用）
- 録音入力は Core Audio、システム音声は ScreenCaptureKit を利用
- 会議データとローカル設定は `~/.seam` に保存

## 機能要件

| ID | 要件 |
| --- | --- |
| REC-01 | マイク音声を録音し、任意でシステム音声を同時に取り込む |
| REC-02 | 録音中の音声をミックスし、再生用音声を保存する |
| TR-01 | Apple Speech の途中結果を録音中のライブプレビューとして表示する |
| TR-02 | Apple Speech の確定結果を順次 UI に配信し、復旧用ファイルに保存する |
| TR-03 | 停止時に残りの音声を解析してから議事録を保存する |
| TR-04 | 保存済み音声を Apple Speech で再文字起こしできる |
| TR-05 | 話者の検出、分離、名前付けを行わない |
| MIN-01 | transcript、要約、音声を議事録として管理する |
| MIN-02 | 議事録の検索、編集、削除、Markdown export を行える |
| SUM-01 | ローカルまたは明示設定した provider で要約を作成する |
| DATA-01 | 既存 `~/.seam` データを再利用し、旧議事録や録音を保持する |

## 非機能要件

- 音声認識を会議終了後まで遅延させず、確定文を発話中に継続表示する。
- 認識 helper と FastAPI の停止処理で WAV を閉じてから残りの音声を解析し、ファイルを確定する。
- API はローカル PC 内で使うことを前提とし、ループバック以外のクライアントを制限する。
- Apple Speech の言語リソースがない場合、OS が必要なリソースを準備してから認識する。
- 旧データの互換列やユーザー音声を、機能削除時に無断で消去しない。

## 配布

- SwiftUI アプリ、Python backend、audio sidecar、Apple Speech helper を含む DMG を生成する。
- リリース workflow は macOS 26 arm64 runner を使う。
