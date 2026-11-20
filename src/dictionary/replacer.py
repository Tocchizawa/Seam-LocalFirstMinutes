"""corrections list を文字起こしセグメントに単純文字列置換で適用する。"""
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
