# アーキテクチャ

## 構成

Seam は macOS ネイティブ UI、ローカル Python API、音声認識ヘルパーで構成する。

| 層 | 実装 | 役割 |
| --- | --- | --- |
| デスクトップ UI | SwiftUI (`macos/Sources/SeamApp`) | 録音操作、ライブ transcript、議事録、設定 |
| 共通クライアント | Swift (`macos/Sources/SeamCore`) | FastAPI 通信、モデル、Apple Speech draft |
| ローカル API | FastAPI (`src`) | 録音調整、保存、検索、要約、復旧 |
| 音声入力 | Core Audio / ScreenCaptureKit sidecar | マイクとシステム音声を取得 |
| 文字起こし | Apple Speech (`AppleSpeechTranscriber`) | 音声を読みながら途中結果と確定結果を JSON Lines で出力 |
| 保存 | SQLite と `~/.seam/sessions` | 議事録、全文検索、音声、復旧用 transcript |

## 録音とライブ文字起こし

1. UI がローカル API に録音開始を依頼する。
2. バックエンドが音声入力を開始し、マイクとシステム音声を 16 kHz mono PCM にミックスする。
3. ミックス音声を `combined.wav` に追記しながら、Apple Speech ヘルパーが同じファイルを追いかけて解析する。
4. Apple Speech の未確定結果はライブプレビューとして WebSocket で UI に送る。確定した発話は `transcript.jsonl` に追記し、UI にも順次送る。
5. 停止時に入力を閉じ、ヘルパーに残りの音声を処理させる。WAV を FLAC に確定し、議事録を SQLite に保存する。
6. 自動要約が有効なら、要約ジョブをバックグラウンドキューに登録する。

認識ヘルパーを録音開始時に起動できなかった場合は、保存した WAV を停止後に認識する。音声を保持した議事録は、後から再文字起こしできる。

## 音声と復旧

各録音は `~/.seam/sessions/<session_id>/` に保存する。録音中の `session_meta.json` と確定 transcript は、異常終了後に録音を救済するために使う。ライブプレビューは一時的な UI 表示であり、確定結果だけを保存する。

録音の終了処理は、録音入力停止、ミキサー停止、WAV クローズ、Apple Speech の入力終端、音声確定、議事録保存の順で進める。UI 側は WebSocket 再接続後にセッション状態を再取得できる。

## プライバシーとネットワーク

文字起こしは macOS の Apple Speech を使用する。API はループバックインターフェースで動作し、アプリの通常利用で音声を外部 API に送信しない。要約は Ollama のほか、利用者が設定した Claude / OpenAI API またはローカル CLI provider を選択できる。

## ビルド

- `swift build --package-path macos --product SeamApp`: SwiftUI アプリ
- `swift build --package-path macos --product AppleSpeechTranscriber`: 文字起こし helper
- `bash scripts/build-native.sh`: Python バックエンド、音声 sidecar、アプリ、DMG をまとめて作成
- `.github/workflows/release-dmg.yml`: タグまたは手動起動で macOS DMG を作成・公開
