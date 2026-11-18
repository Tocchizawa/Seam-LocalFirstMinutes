# Refactor Audit (2026-05-19)

対象: 録音/文字起こし/再生まわりの現状調査  
目的: 冗長処理・責務重複・保守リスクの洗い出し

## 実施状況
- [x] 優先度A-1 セッション復旧ロジックの同期/非同期重複を一本化
- [x] 優先度A-2 チャンク時刻算出ロジックの共通化
- [ ] 優先度B以降

## 優先度A（先に手を付ける）

### 1) セッション復旧ロジックの同期版/非同期版がほぼ二重実装
- 該当:
  - `src/api/recording.py:1070` `def _recover_one_session(...)`
  - `src/api/recording.py:1177` `async def _recover_one_session_async(...)`
  - `src/api/recording.py:1280` `def recover_pending_sessions(...)`
  - `src/api/recording.py:1313` `async def recover_pending_sessions_async(...)`
- 問題:
  - ロジック差分が今後入りやすく、復旧結果が呼び出し経路で変わる危険がある。
  - 実際の起動経路は async 側 (`src/main.py:242-247`) で、sync 側は実質デッドに近い。
- 方針:
  - 共通本体を1つに統一（`rediarize` 部分だけ `to_thread` の有無を引数制御）。
  - `recover_pending_sessions` は互換用途が不要なら削除、必要なら thin wrapper 化。

### 2) チャンク時刻算出ロジックが streaming と retranscribe で重複
- 該当:
  - `src/transcribe/streaming.py:1026-1063` (`_chunker_pending_sec` + `_enqueue`)
  - `src/api/minutes.py:505-534` (`_chunker_pending_sec` + `_append_job`)
- 問題:
  - タイムスタンプ計算ルールが2箇所で進化すると、ライブと再文字起こしでズレる。
  - バグ修正時に片方だけ直る事故が起きやすい。
- 方針:
  - 共通ユーティリティ化（例: `src/transcribe/timeline.py`）し、両方から利用。

## 優先度B（次に整理）

### 3) WS再接続とイベント分配ロジックが複数箇所で重複
- 該当:
  - `gui/src/pages/DetailView.tsx:115-128`（live時のWS接続）
  - `gui/src/pages/DetailView.tsx:410-423`（非live時のWS接続）
  - `gui/src/lib/recording-context.tsx:206-224`（全体WS接続）
- 問題:
  - reconnect/backoff 実装が分散し、挙動差や修正漏れが起きやすい。
  - `speaker_renamed` など同種イベントの適用実装も分散。
- 方針:
  - `useRecordingWs` 的な共通フックを作り、接続・再接続・json parse を一本化。

### 4) DetailView が巨大で責務混在（表示/再生/要約/検索/WS）
- 該当:
  - `gui/src/pages/DetailView.tsx` 全体（`1890`行）
- 問題:
  - 一箇所改修で副作用が出やすい。
  - テスト可能な最小単位に分解しづらい。
- 方針:
  - まず「データ購読層」と「表示コンポーネント層」を分離。
  - 例: `useDetailMinutesState`, `useDetailPlayback`, `useDetailFind` へ抽出。

### 5) 音声ファイル選択ルールが用途別に分散
- 該当:
  - `src/api/recording.py:182-199` `_pick_playback_audio`
  - `src/api/recording.py:1047-1052` `_pick_recovery_wav`
- 問題:
  - 優先順位変更時に片方だけ更新される危険。
- 方針:
  - 「再生用」「復旧用」の差分だけパラメータ化し、選択エンジンを共通化。

## 優先度C（余力で整理）

### 6) ffmpeg 解決ロジックが2系統
- 該当:
  - `src/main.py:93-110` `_ensure_ffmpeg_on_path`
  - `src/audio/recorder.py:30-59` `_find_ffmpeg`
- 問題:
  - PATH 補正と実行パス解決の責務が別管理で、環境差異時に追跡が難しい。
- 方針:
  - ffmpeg 解決を1モジュールに集約して、main/recorder で共通利用。

## 推奨リファクタ順
1. A-1（復旧ロジック一本化）
2. A-2（時刻算出ロジック共通化）
3. B-3（WS接続共通化）
4. B-4（DetailView分割）
5. B-5 / C-6（選択ルール・ffmpeg整理）

## 補足
- 直近の timestamp/playback 修正はコミット済み: `f90a49d`
- 次段の refactor は「動作差分なし」を前提に、単位を小さく分けて進めるのが安全。
