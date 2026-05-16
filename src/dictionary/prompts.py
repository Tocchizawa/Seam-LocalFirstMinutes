"""辞書 (用語抽出 / 誤転写補正) 用のプロンプト。"""
from __future__ import annotations

import re

GLOSSARY_EXTRACT_SYSTEM_PROMPT = """あなたはプロジェクトドキュメントから「会議で頻出する固有名詞・用語」を抽出するアシスタントです。

# 出力形式 (JSON のみ。前置きや説明を一切付けない)
{
  "glossary": [
    {"term": "美里子", "description": "ミラモル主人公キャラクター"},
    {"term": "道しるべ", "description": "ナビゲーション機能"}
  ]
}

# 抽出ルール
- 一般名詞ではなく、このプロジェクト固有 / 専門 / カタカナ複合 / 固有名詞を優先
- 1単語に絞らず、明確に1まとまりで意味を成すもの (例: "AIチャット相談" など複合)
- description は10-30文字程度。簡潔に
- 最大30件
- 似た用語の重複は避ける
"""


def build_glossary_extract_user_prompt(docs_text: str, project_name: str = "") -> str:
    header = f"# プロジェクト: {project_name}\n\n" if project_name else ""
    return (
        header
        + "# 参考ドキュメント\n"
        + docs_text.strip()
        + "\n\n上記から会議で頻出しそうな用語を抽出して JSON で返してください。"
    )


def build_glossary_extract_user_prompt_agentic(
    project_name: str,
    doc_paths: list[str],
    existing_glossary: list[str] | None = None,
) -> str:
    """CLI agent (claude_code/codex) 用。docs は inline せず、自分で読みに行かせる。"""
    parts: list[str] = []
    if project_name:
        parts.append(f"# プロジェクト: {project_name}")
    parts.append(
        "# タスク\n"
        "下記のディレクトリ配下のドキュメント (Markdown / テキスト / コードコメント等) を"
        "Read / Glob / Grep ツールで **自由に・徹底的に** 探索し、会議で頻出する用語を抽出してください。\n"
        "時間制限は気にせず、必要なら 5-10 分かけて深く読み込んで構いません。"
    )
    parts.append(
        "# 探索対象ディレクトリ\n"
        + "\n".join(f"- {p}" for p in doc_paths)
    )
    if existing_glossary:
        parts.append(
            "# 既に登録済みの用語 (これら以外で新規に発見したもののみ返す)\n"
            + "\n".join(f"- {g}" for g in existing_glossary)
        )
    parts.append(
        "# 完了条件\n"
        "- README / KNOWLEDGE / GLOSSARY 等の主要ドキュメントを少なくとも1度は確認した\n"
        "- ディレクトリ構造を把握し、独自概念が登場するセクションを特定した\n"
        "- 用語30件以内、descriptionを付けてJSONで返す"
    )
    return "\n\n".join(parts)


# ─── Phase B (要約後の辞書整理): glossary 追加 + corrections を1回で抽出 ───

DICTIONARY_UPDATE_SYSTEM_PROMPT = """あなたは会議議事録の辞書整理アシスタントです。
直近の会議の文字起こしと、プロジェクトのドキュメントを照合して、次回以降の文字起こし精度を上げるための辞書を整理します。

# あなたが行うこと
1. 文字起こし中に登場した固有名詞・専門用語のうち、まだ登録されていないものを `new_glossary` に追加
2. 文字起こしに含まれる Whisper の誤転写 (固有名詞・専門用語が音韻の近い別表記になっているもの) を `new_corrections` に追加

# 出力形式 (JSON のみ。前置きや説明を一切付けない)
{
  "new_glossary": [
    {"term": "看取り期", "description": "介護フェーズの一つ。終末期"}
  ],
  "new_corrections": [
    {"wrong": "見取り機", "correct": "看取り期", "confidence": 0.95, "reason": "音韻が近く文脈から看取り期が妥当"}
  ]
}

# 抽出ルール (glossary)
- transcript に登場、かつ 既存 glossary に含まれない、かつ プロジェクト固有 / 専門用語 / 略語 / 固有名詞
- 一般語は除外。description は10-30文字
- 最大15件

# 抽出ルール (corrections)
- ドキュメントの正式表記と「明らかに音韻が近いがズレている」transcript 上の語に限る
- 一般語の表記ゆれ (子供↔子ども等) は含めない
- confidence は 0.0-1.0。確信できないものは出力しない
- wrong / correct が同一の場合は出力しない
- 最大15件
"""


def build_dictionary_update_user_prompt_inline(
    transcript: str,
    *,
    project_name: str,
    docs_text: str,
    existing_glossary: list[str],
    existing_corrections: list[dict],
) -> str:
    """API provider 用 (Ollama / Claude API / OpenAI)。docs を inline で渡す。"""
    parts: list[str] = []
    if project_name:
        parts.append(f"# プロジェクト: {project_name}")
    if existing_glossary:
        parts.append(
            "# 既存 glossary (これら以外で新規発見したもののみ new_glossary に入れる)\n"
            + "\n".join(f"- {g}" for g in existing_glossary)
        )
    if existing_corrections:
        parts.append(
            "# 既知の誤転写ペア (これら以外で新規発見したもののみ new_corrections に入れる)\n"
            + "\n".join(
                f"- {p.get('wrong', '')} → {p.get('correct', '')}"
                for p in existing_corrections
            )
        )
    if docs_text:
        parts.append("# 参考ドキュメント抜粋\n" + docs_text.strip())
    parts.append("# 文字起こし\n" + transcript)
    parts.append(
        "上記をもとに辞書整理を行い、`new_glossary` と `new_corrections` を含む JSON で返してください。"
    )
    return "\n\n".join(parts)


def build_dictionary_update_user_prompt_agentic(
    transcript: str,
    *,
    project_name: str,
    doc_paths: list[str],
    existing_glossary: list[str],
    existing_corrections: list[dict],
) -> str:
    """CLI agent 用。docs は inline せず Read で取りに行かせる。"""
    parts: list[str] = []
    if project_name:
        parts.append(f"# プロジェクト: {project_name}")
    parts.append(
        "# タスク\n"
        "下記の文字起こしを読み、ドキュメントディレクトリを **Read / Glob / Grep で自由に探索** して照合し、"
        "次回以降の Whisper 文字起こし精度を上げるための辞書整理を行ってください。"
    )
    parts.append(
        "# 探索可能なドキュメントディレクトリ\n"
        + "\n".join(f"- {p}" for p in doc_paths)
    )
    if existing_glossary:
        parts.append(
            "# 既存 glossary (これら以外で新規発見したもののみ new_glossary に入れる)\n"
            + "\n".join(f"- {g}" for g in existing_glossary)
        )
    if existing_corrections:
        parts.append(
            "# 既知の誤転写ペア (これら以外で新規発見したもののみ new_corrections に入れる)\n"
            + "\n".join(
                f"- {p.get('wrong', '')} → {p.get('correct', '')}"
                for p in existing_corrections
            )
        )
    parts.append("# 文字起こし\n" + transcript)
    parts.append(
        "ドキュメントを深く読み込んだうえで `new_glossary` と `new_corrections` を含む JSON で返してください。"
        "時間制限は気にせず、必要なら 5-10 分かけて構いません。"
    )
    return "\n\n".join(parts)


# LLM 出力は時々 ```json``` でラップされるので除去
_JSON_BLOCK_PATTERN = re.compile(
    r"```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```",
    re.MULTILINE,
)


def extract_json_object(text: str) -> str | None:
    """LLM 出力から JSON オブジェクト本文を抽出する。

    1. ``` ... ``` のフェンスがあれば中身を返す
    2. なければ最初の `{` から対応する `}` までをグループとして取得
    """
    if not text:
        return None
    m = _JSON_BLOCK_PATTERN.search(text)
    if m:
        return m.group(1)
    # `{` の位置を探して、対応 `}` までをスキャン
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None
