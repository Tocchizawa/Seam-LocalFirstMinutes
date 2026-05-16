"""corrections list を文字起こしセグメントに単純文字列置換で適用する。

- LLM 不要 (高速・予測可能)
- transcript の text フィールドだけを書き換える
- Whisper への initial_prompt にも正式表記を入れることでさらに精度上昇
"""
from __future__ import annotations


def apply_corrections(
    segments: list[dict],
    corrections: list[dict],
) -> tuple[list[dict], int]:
    """transcript 各セグメントの text フィールドに置換を適用。

    Args:
        segments: [{"text": str, ...}, ...]
        corrections: [{"wrong": str, "correct": str}, ...]

    Returns:
        (置換後 segments のリスト, 置換した出現回数の合計)
    """
    if not corrections or not segments:
        return segments, 0
    # 長いキーから順に置換 (短いキーが部分一致して長いキーを破壊しないように)
    pairs = sorted(
        [
            (p.get("wrong", ""), p.get("correct", ""))
            for p in corrections
            if p.get("wrong") and p.get("correct") and p.get("wrong") != p.get("correct")
        ],
        key=lambda x: len(x[0]),
        reverse=True,
    )
    if not pairs:
        return segments, 0

    total = 0
    out: list[dict] = []
    for seg in segments:
        text = seg.get("text", "")
        if not text:
            out.append(seg)
            continue
        new_text = text
        seg_replaced = 0
        for wrong, correct in pairs:
            if wrong in new_text:
                count = new_text.count(wrong)
                new_text = new_text.replace(wrong, correct)
                seg_replaced += count
        if seg_replaced > 0:
            cloned = dict(seg)
            cloned["text"] = new_text
            out.append(cloned)
            total += seg_replaced
        else:
            out.append(seg)
    return out, total


def build_initial_prompt_with_corrections(
    glossary: list[str], corrections: list[dict],
) -> str:
    """Whisper への initial_prompt を組み立てる。

    glossary に正式表記を追加することで、Whisper が誤転写しがちな部分を予防する。
    corrections の "correct" 側も含めて、正式表記をすべて Whisper に提示。
    """
    terms: set[str] = set()
    for g in glossary:
        if not g:
            continue
        # "用語: 説明" 形式の場合は用語部分のみ
        head = g.split(":", 1)[0].split("：", 1)[0].strip()
        if head:
            terms.add(head)
    for p in corrections:
        c = (p.get("correct") or "").strip()
        if c:
            terms.add(c)
    if not terms:
        return ""
    # 句読点で区切って一文にする
    return "、".join(sorted(terms))
