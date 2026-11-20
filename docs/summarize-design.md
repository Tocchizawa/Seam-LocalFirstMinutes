# 要約設計

## 入力

要約処理は、議事録に保存された確定 transcript、プロジェクト情報、用語集、設定済み provider を使う。ライブプレビューは未確定のため要約入力に含めない。

## 処理

1. SQLite から議事録と transcript を取得する。
2. transcript が長い場合は、既存の要約パイプラインでチャンクごとに処理する。
3. 選択された provider で要約と必要なタイトルを生成する。
4. 結果を議事録へ保存し、必要に応じて辞書更新を行う。

利用できる provider は Ollama、Claude API、OpenAI API、Claude Code CLI、Codex CLI である。利用者が有効にした場合のみ、外部 API provider に要約内容を送る。

## Transcript の扱い

文字起こし segment は開始・終了時刻と本文を持ち、話者 ID や名前を必要としない。要約プロンプトも発話者を推定・付与せず、入力 transcript の内容に基づいて議題、決定事項、課題、アクションをまとめる。

用語集と corrections は transcript の後処理や要約後の辞書更新に利用する。文字起こしエンジンへ事前用語プロンプトを渡す処理はない。
