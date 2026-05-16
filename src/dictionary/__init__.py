"""辞書 (用語集 + 誤転写補正) サブシステム。

機能:
  - ``extractor``: project の doc_dirs から canonical 用語を LLM で抽出 → glossary 提案
  - ``corrector``: 文字起こしと glossary/docs を照合し、Whisper 誤転写ペアを LLM で発見
  - ``replacer``: corrections list を transcript に単純文字列置換で適用

設計方針:
  - 補正は要約完了後に自動実行 (確認なし、過去minutesは触らない)
  - 発見した wrong→correct は project.corrections に蓄積 → 将来の録音に効く
"""

from .replacer import apply_corrections, build_initial_prompt_with_corrections

__all__ = ["apply_corrections", "build_initial_prompt_with_corrections"]
