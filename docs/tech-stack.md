# 技術スタック

| 領域 | 技術 | 用途 |
| --- | --- | --- |
| デスクトップ UI | SwiftUI | macOS アプリ |
| Swift 共通コード | Swift Package Manager | API client、状態モデル、Apple Speech 関連コード |
| 音声認識 | Apple Speech / SpeechAnalyzer | macOS 上でのライブ認識 |
| ローカル API | Python 3.11+、FastAPI、Uvicorn | API、WebSocket、録音と議事録処理 |
| 音声処理 | Core Audio、ScreenCaptureKit、NumPy、SciPy、FFmpeg | 入力、ミキシング、形式変換 |
| 永続化 | SQLite、YAML、macOS Keychain | 議事録、検索、設定、認証情報 |
| 要約 | Ollama、Claude / OpenAI API、Claude Code / Codex CLI | 議事録要約 |
| Python 環境 | uv | 依存解決とバックエンド実行 |
| 配布 | SwiftPM、codesign、hdiutil、GitHub Actions | ネイティブアプリと DMG |

## 文字起こしのデータフロー

録音ミキサーが 16 kHz mono PCM を `combined.wav` に追記する。Apple Speech helper は WAV の増加部分を読み、SpeechAnalyzer に短いフレームで渡す。未確定の認識文はプレビュー、確定した認識文は transcript chunk として FastAPI に JSON Lines で返す。

FastAPI は確定 chunk を `transcript.jsonl` に追記し、WebSocket で接続中の UI に配信する。停止時には WAV の終端まで解析を続け、認識結果を保存した後に FLAC へ確定する。旧録音の再文字起こしでは音声を PCM WAV に正規化し、同じ Apple Speech helper をファイルモードで実行する。

話者識別・話者分離は行わない。新しい transcript は時刻と本文だけを持つ。従来データ中の追加フィールドは読み飛ばし、SQLite の既存列はデータ移行で削除しない。

## 主な配置

```text
macos/Sources/SeamApp/             SwiftUI screens and app lifecycle
macos/Sources/SeamCore/            API client, models, AppleSpeechDraft
macos/Sources/AppleSpeechTranscriber/  File-following Apple Speech process
sidecar/audio-capture/              ScreenCaptureKit system audio source
src/api/                            FastAPI routes and recording orchestration
src/audio/                          Recording, mixer, audio writer and conversion
src/transcribe/apple_speech.py      Python to Swift helper bridge
src/storage/                        SQLite and Markdown export
scripts/build-native.sh             Local native app and DMG build
```

## Runtime requirements

The app uses Apple Speech APIs introduced for macOS 26, so the packaged app requires macOS 26 or later. The native build targets Apple Silicon. `build-native.sh` packages `uv`, the Python source bundle, audio sidecar, and Apple Speech helper inside the app.
