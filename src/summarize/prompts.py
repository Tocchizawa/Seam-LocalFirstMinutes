"""要約プロンプトテンプレート + token見積り。

provider 共通でプロンプトを組み立てる。
各 provider はこのモジュールから ``build_messages`` / ``estimate_tokens_jp`` を呼ぶ。
"""
from __future__ import annotations

from .base import ProjectContext, SummaryError, SummaryErrorCode

SYSTEM_PROMPT = """あなたは日本語の会議議事録作成アシスタントです。
以下の文字起こしから、構造化された詳細な議事録を Markdown で作成してください。

# 重要な制約
- 思考過程・前置き・「以下に作成します」等の説明文は一切出力しない
- 出力は ## から始まる Markdown 見出しのみで構成する
- 文字起こしに無い情報を捏造しない
- 文字起こし内の話者ラベル (例: 田中, 話者2) はそのまま使用する
- 専門用語は glossary に従って表記を統一する
- 各項目には背景・根拠・経緯を 1文添える(なぜそうなったか、何が論点か)
- 会議内容を漏れなく拾うこと。長さの上限は設けない

# 読みやすさルール (必ず守る)
- 各見出し (##) の **直後と直前は必ず空行** を 1行入れる (見出し前後に空行が無いと詰まって読みにくい)
- 段落と段落の間も **空行 1行** で区切る (連続改行 1行のみは段落区切りにならない)
- 1段落が 3-4 文を超えそうなら段落を分ける
- 箇条書き (-) の内側で複数文書く場合は、文の区切りで **末尾に 半角スペース2つ** を入れて折り返す(Markdown の hard break) — ベタ書きで長くしない
- 1項目の中で改行が必要な補足は、項目の下に **2スペースインデント** で次行を書く
  - 例:
    `- [担当] 〇〇を実装する (期限: 2026-05-20)`
    `  └ 理由: 既存仕様だと負荷が懸念されるため。〇〇 API のレスポンスを軽量化する想定。`
- セクション内で論点が複数ある場合、見出し直下に **太字 1行** の小見出しを使ってブロック分けしてよい
- **太字** や `インラインコード` を使って固有名詞・数値・キーワードを目立たせる(目視スキャンしやすくなる)

# 必須セクション(この順序で出力。空でも省略不可、内容が無い場合は「(該当なし)」と明記)

## 概要

会議の目的・主要な論点・結論・次のアクションを **2-3 段落** で記述する。
各段落は 3-4 文程度に収め、論点ごとに段落を分ける。
箇条書きにせず、読み物として通読できる文章で書くこと。
合計 300-500 字を目安にする。

## 決定事項

- 全ての決定事項を漏れなく箇条書きで抽出する
- 形式: `- **〈決定内容〉** — 〈背景・理由〉(決定者: 〈名〉, 期限: YYYY-MM-DD)`
- 1項目が長くなる場合は、2行目を 2スペースインデントで補足し、行末スペース 2 つで段落内改行する
- 決定者・期限が文字起こしから判明しない場合は該当部分を省略してよい
- 「決定」だけでなく、合意・確認・方針確定も含める

## TODO

- 形式: `- [〈担当者〉] 〈タスク内容〉 (期限: YYYY-MM-DD)`
  - 2行目以降は 2スペースインデントで「なぜ必要か」「前提となる完了条件」を 1-2 文補足
- 担当者不明なら `[未定]`、期限不明なら期限部分を省略
- 文字起こしから推測可能な ToDo はすべて拾う(明示的に「やります」と発言してなくても、文脈上タスク化されているものは含める)

## 議論ハイライト

- 主要トピックごとに項目を立てる。件数の上限は設けない(内容に応じて 5-15項目程度)
- 形式: `- **〈論点・テーマ〉** — 〈議論された内容・対立軸・反対意見・判断の経緯〉(主な発言者: 〈名〉)`
- 内容が長い場合は 2スペースインデントで段落を続けてよい
- 単純な事実報告ではなく、「なぜ議論になったか」「どんな選択肢が比較されたか」「どう判断されたか」を含める
- 同じ話題が複数箇所で言及されていれば 1項目にまとめる(冗長禁止)

# 任意セクション(該当する内容が文字起こしに含まれていれば、必須セクションの後に追加する)

## 背景・前提

会議に至る経緯、関連する過去の決定、参加者間で共有されている前提など。

## 次回までの宿題・確認事項

TODO とは別に「持ち越し」「次回までに確認」となった項目。

## 懸念・リスク

未解決の懸念、想定される失敗、後で問題になりそうな点。

## 共有事項・連絡

タスクや決定事項ではないが、参加者に共有された情報・進捗・他チームの状況など。

# 留意点
- 雑談・脱線・本筋に無関係な発言は議事録に含めない
- 抽象的なまとめより、具体的な数字・固有名詞・期限・条件を優先して記述する
- 「〜について議論した」のような中身の無い表現は使わない。何がどう議論されたかを書く
- **見出し前後の空行と段落間の空行は絶対に省略しない** (詰まった出力は不可)
"""


# タイトル生成専用プロンプト (要約 markdown とは別リクエストで使う)
TITLE_SYSTEM_PROMPT = """あなたは会議議事録のタイトルを命名するアシスタントです。

# 出力形式
- タイトル本文1行のみを出力する
- 引用符・句読点・「タイトル: 」等の接頭辞・改行・前置き・末尾コメント を一切付けない
- 全角30文字以内 (短く具体的に)
- 議事録の時刻情報や日付は含めない

# 良い例
タグ機能リリース前進捗確認
ミラモル イラスト方向性レビュー

# 悪い例 (出してはいけない)
「タグ機能リリース前進捗確認」     ← 引用符
タイトル: タグ機能リリース前進捗確認  ← 接頭辞
2026年5月10日 タグ機能進捗確認会     ← 日付入り
"""


def build_title_messages(
    transcript: str, project: ProjectContext | None = None,
) -> tuple[str, str]:
    """タイトル生成用の (system, user) を返す。

    要約と分離した小さい呼出。短い transcript を渡してタイトル1行を生成させる。
    """
    p = project or ProjectContext()
    project_name = p.name or ""
    members = _format_members(p.members)
    user = (
        (f"# プロジェクト: {project_name}\n\n" if project_name else "")
        + (f"# 参加者\n{members}\n\n" if p.members else "")
        + f"# 文字起こし\n{transcript}\n\n"
        "上記会議のタイトルを30文字以内で1行だけ出力してください。"
    )
    return TITLE_SYSTEM_PROMPT, user


# タイトル生成に渡す transcript 長の上限 (token節約)。
# 先頭/末尾を抽出して LLM に投げる。
TITLE_TRANSCRIPT_HEAD_CHARS = 1500
TITLE_TRANSCRIPT_TAIL_CHARS = 800


def truncate_transcript_for_title(transcript: str) -> str:
    if not transcript:
        return ""
    n = len(transcript)
    if n <= TITLE_TRANSCRIPT_HEAD_CHARS + TITLE_TRANSCRIPT_TAIL_CHARS + 100:
        return transcript
    head = transcript[:TITLE_TRANSCRIPT_HEAD_CHARS]
    tail = transcript[-TITLE_TRANSCRIPT_TAIL_CHARS:]
    return f"{head}\n\n[... 中略 ...]\n\n{tail}"


def normalize_generated_title(raw: str, max_length: int = 30) -> str | None:
    """LLM 出力から余計な装飾を除去してタイトル文字列にする。"""
    if not raw:
        return None
    # 改行で複数行になった場合、最初の非空行
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return None
    title = lines[0]
    # 引用符・接頭辞を除去
    title = title.strip("「」『』\"'`")
    for prefix in (
        "タイトル:", "タイトル:", "タイトル ", "Title:", "title:",
    ):
        if title.startswith(prefix):
            title = title[len(prefix):].strip()
    # 長さを切る (max_length 文字超え)
    if len(title) > max_length:
        title = title[:max_length]
    if not title:
        return None
    return title


def _format_members(members: list[dict[str, str]]) -> str:
    if not members:
        return "(指定なし)"
    lines: list[str] = []
    for m in members:
        name = str(m.get("name", "")).strip()
        if not name:
            continue
        role = str(m.get("role", "")).strip()
        lines.append(f"- {name} ({role})" if role else f"- {name}")
    return "\n".join(lines) if lines else "(指定なし)"


def _format_glossary(glossary: list[str]) -> str:
    if not glossary:
        return "(指定なし)"
    cleaned = [g for g in (str(x).strip() for x in glossary) if g]
    if not cleaned:
        return "(指定なし)"
    return "\n".join(f"- {g}" for g in cleaned)


def _discover_known_files(p: ProjectContext) -> list[str]:
    """doc_dirs 配下に存在する「重要ファイル」の絶対パスを返す。

    agent に「これを読め」と具体的な absolute path を渡すことで、
    discover (Glob) のステップを省略させ、Read を即実行する確率を上げる。
    """
    import os

    known_names = ("KNOWLEDGE.md", "glossary.md", "GLOSSARY.md", "README.md")
    found: list[str] = []
    for d in p.doc_dirs:
        if not d or not os.path.isdir(d):
            continue
        for name in known_names:
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and cand not in found:
                found.append(cand)
    # repo_path 直下の README.md も
    if p.repo_path and os.path.isdir(p.repo_path):
        for name in ("README.md", "KNOWLEDGE.md"):
            cand = os.path.join(p.repo_path, name)
            if os.path.isfile(cand) and cand not in found:
                found.append(cand)
    return found


def _format_docs_hint(p: ProjectContext) -> str:
    """CLI agent に対し、最初に Read tool で specific files を読むよう命令する。

    実装方針:
      - 抽象パスではなく **既存ファイルの絶対パス** を渡す (発見コスト削減)
      - 命令は「STEP 1」として短く、強く、最初に置く
      - ツールを持たない provider 用に「ツール無しなら飛ばせ」と保険
    """
    must_read_files = _discover_known_files(p)
    if not must_read_files:
        # 既知ファイルが何も無いなら hint 自体スキップ
        return ""

    bullet = "\n".join(f"- {f}" for f in must_read_files)
    # User prompt の冒頭に置かれる想定。"STEP 1" を最初に、命令的に。
    return (
        "STEP 1 (REQUIRED, before summarizing): If you have the `Read` tool, "
        "read the following file(s) using the Read tool. "
        "Use the absolute paths exactly as shown. "
        "If you do not have the Read tool (e.g. plain API mode), skip this step.\n"
        f"{bullet}\n\n"
        "After reading, use the document content for:\n"
        "- 正式表記 (project / stakeholder names)\n"
        "- 用語集 (glossary)\n"
        "- 人物の役職や所属の整合\n"
        "Do NOT copy the doc content into the summary; it is reference only.\n\n"
        "STEP 2: Then create the meeting minutes summary as instructed.\n"
        "─────────────────────────────────────────────\n"
    )


def build_user_prompt(transcript: str, project: ProjectContext | None = None) -> str:
    """provider 共通の user prompt を組み立てる。

    docs_hint は最も agent に届きやすいよう **最先頭** に配置する。
    """
    p = project or ProjectContext()
    project_name = p.name or "(プロジェクト未指定)"
    members = _format_members(p.members)
    glossary = _format_glossary(p.glossary)
    docs_hint = _format_docs_hint(p)
    sections: list[str] = []
    if docs_hint:
        # 命令を最先頭に配置 (recency/primacy 両方狙い)
        sections.append(docs_hint.rstrip())
    sections.append(f"# プロジェクト: {project_name}")
    sections.append(f"# 参加者\n{members}")
    sections.append(f"# 用語集\n{glossary}")
    sections.append(f"# 文字起こし\n{transcript}")
    return "\n\n".join(sections) + "\n"


def build_messages(
    transcript: str,
    project: ProjectContext | None = None,
) -> tuple[str, str]:
    """要約用の (system_prompt, user_prompt) を返す。

    タイトル生成は別リクエスト (build_title_messages) で行うため、
    ここでは要約本体のみ生成する prompt を返す。
    """
    return SYSTEM_PROMPT, build_user_prompt(transcript, project)


# ─── Token見積り ────────────────────────────────────────────

# 日本語混在テキストの粗い目安。
# Whisper transcript は密度が高めなので 1 char ≒ 0.7-0.8 token。
# 安全側 (= 大きめ) に評価するため小さい除数を採用。
_CHARS_PER_TOKEN_JP = 1.3


def estimate_tokens_jp(text: str) -> int:
    """日本語混在テキストの token 数概算。Ollama / Claude / OpenAI / Gemini 共通の粗い目安。

    厳密な tiktoken 等は依存重いので採用しない。Ollama の num_ctx 検証用途で
    送信前に「明らかに溢れる」ケースを弾けば十分。
    """
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN_JP) + 1


# system + user prompt のテンプレ部分の固定オーバーヘッド (大体)
PROMPT_OVERHEAD_TOKENS = 600

# 出力に確保しておきたい余裕 (議事録1本)
# Qwen3 等の thinking モデルは内部 reasoning に多く使うため、
# 実際の議事録テキスト (~1500 tokens) + 思考バッファ (~6500) で 8192 を確保。
DEFAULT_MAX_OUTPUT_TOKENS = 8192

# 安全マージン
SAFETY_MARGIN_TOKENS = 256


def validate_context_budget(
    transcript: str,
    *,
    ctx_window: int,
    max_output: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> int:
    """送信前の context 長検証。超過なら SummaryError(CONTEXT_OVERFLOW) を raise。

    Returns:
        実際に必要な token数 (検証OK時)
    """
    needed = (
        estimate_tokens_jp(transcript)
        + PROMPT_OVERHEAD_TOKENS
        + max_output
        + SAFETY_MARGIN_TOKENS
    )
    if needed > ctx_window:
        raise SummaryError(
            SummaryErrorCode.CONTEXT_OVERFLOW,
            (
                f"文字起こしが長すぎます (約 {needed} tokens 必要、"
                f"現在の上限 {ctx_window} tokens)。"
                f"設定でコンテキスト長を増やすか、Claude/Gemini など"
                "長コンテキスト対応のproviderを利用してください。"
            ),
        )
    return needed


# ─── 文字起こしを provider 入力用に整形 ────────────────────

def format_transcript_segments(segments: list[dict]) -> str:
    """transcript (DB保存形式の list[dict]) を ``[mm:ss] (話者) text`` 形式に整形する。

    Args:
        segments: [{"start": float, "end": float, "text": str,
                   "speaker_label": str | None}, ...]
    """
    lines: list[str] = []
    for seg in segments:
        start = float(seg.get("start", 0.0))
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        mm = int(start // 60)
        ss = int(start % 60)
        timestamp = f"[{mm:02d}:{ss:02d}]"
        label = str(seg.get("speaker_label") or "").strip()
        if label:
            lines.append(f"{timestamp} ({label}) {text}")
        else:
            lines.append(f"{timestamp} {text}")
    return "\n".join(lines)


# ─── 短すぎる文字起こしの判定 ──────────────────────────────

# 50 char 未満の transcript は要約スキップ (発話なし扱い)
MIN_TRANSCRIPT_CHARS_FOR_SUMMARY = 50


def is_too_short_for_summary(transcript: str) -> bool:
    return len(transcript.strip()) < MIN_TRANSCRIPT_CHARS_FOR_SUMMARY
