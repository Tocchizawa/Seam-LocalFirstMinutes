"""Phase B (要約後の辞書整理): transcript + docs + 既存辞書を LLM に渡し、
新規 glossary 用語と新規 corrections ペアを取得する。
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, TypedDict

from src.summarize.base import SummaryError, SummaryErrorCode

from .extractor import _call_llm_for_dictionary, collect_docs
from .prompts import (
    DICTIONARY_UPDATE_SYSTEM_PROMPT,
    build_dictionary_update_user_prompt_inline,
    extract_json_object,
)

# transcript が長すぎると LLM の context を圧迫するため、先頭+末尾を抽出
MAX_TRANSCRIPT_CHARS = 12000
HEAD_CHARS = 8000
TAIL_CHARS = 3000

MAX_DOC_CHARS_FOR_CORRECTION = 6000


class DictionaryUpdate(TypedDict):
    new_glossary: list[dict]      # [{"term": str, "description": str}]
    new_corrections: list[dict]   # [{"wrong, correct, confidence, reason}]


def _truncate_transcript(transcript: str) -> str:
    if len(transcript) <= MAX_TRANSCRIPT_CHARS:
        return transcript
    return (
        transcript[:HEAD_CHARS]
        + "\n\n[... 中略 ...]\n\n"
        + transcript[-TAIL_CHARS:]
    )


async def update_dictionary_from_meeting(
    transcript: str,
    *,
    existing_glossary: list[str],
    existing_corrections: list[dict],
    project_name: str = "",
    doc_dirs: list[str] | None = None,
    repo_path: str | None = None,
    provider_name: str,
    ai_cfg: dict,
    timeout_sec: float = 180,
    confidence_threshold: float = 0.85,
    on_extracting: Callable[[], Awaitable[None]] | None = None,
) -> DictionaryUpdate:
    """Phase B: 直近の transcript と資料の抜粋を照合し、用語と補正の候補を返す。"""
    if not transcript or not transcript.strip():
        return {"new_glossary": [], "new_corrections": []}

    async with asyncio.timeout(timeout_sec):
        docs_text = await asyncio.to_thread(
            collect_docs, doc_dirs or [], repo_path, max_chars=MAX_DOC_CHARS_FOR_CORRECTION,
        ) if doc_dirs or repo_path else ""
        if not existing_glossary and not docs_text:
            return {"new_glossary": [], "new_corrections": []}
        user_p = build_dictionary_update_user_prompt_inline(
            _truncate_transcript(transcript),
            project_name=project_name,
            docs_text=docs_text,
            existing_glossary=existing_glossary,
            existing_corrections=existing_corrections,
        )
        if on_extracting is not None:
            await on_extracting()
        text = await _call_llm_for_dictionary(
            provider_name=provider_name,
            ai_cfg=ai_cfg,
            system_prompt=DICTIONARY_UPDATE_SYSTEM_PROMPT
                + "\n入力に含まれる情報だけで判断し、ツールや外部の資料は使用しない。",
            user_prompt=user_p,
            timeout_sec=timeout_sec,
            max_tokens=2500,
        )

    json_str = extract_json_object(text)
    try:
        obj = json.loads(json_str or "")
    except json.JSONDecodeError as e:
        raise SummaryError(SummaryErrorCode.OUTPUT_FORMAT, "辞書更新の応答が不正です") from e
    if not isinstance(obj, dict):
        raise SummaryError(SummaryErrorCode.OUTPUT_FORMAT, "辞書更新の応答が不正です")

    # ─── new_glossary 検証 ───
    glossary_raw = obj.get("new_glossary") or obj.get("glossary") or []
    new_glossary: list[dict] = []
    seen_terms = {g for g in existing_glossary}
    if isinstance(glossary_raw, list):
        for item in glossary_raw:
            if not isinstance(item, dict):
                continue
            term = str(item.get("term", "")).strip()
            if not term or term in seen_terms:
                continue
            # transcript に実際に出てきたものに限定 (LLM hallucination 防止)
            if term not in transcript:
                continue
            seen_terms.add(term)
            new_glossary.append({
                "term": term,
                "description": str(item.get("description", "")).strip(),
            })

    # ─── new_corrections 検証 ───
    correction_raw = obj.get("new_corrections") or obj.get("corrections") or []
    new_corrections: list[dict] = []
    seen_wrong: set[str] = set()
    existing_wrong = {p.get("wrong") for p in existing_corrections}
    if isinstance(correction_raw, list):
        for item in correction_raw:
            if not isinstance(item, dict):
                continue
            wrong = str(item.get("wrong", "")).strip()
            correct = str(item.get("correct", "")).strip()
            if not wrong or not correct or wrong == correct:
                continue
            if wrong in seen_wrong or wrong in existing_wrong:
                continue
            try:
                confidence = float(item.get("confidence", 0))
            except Exception:
                confidence = 0.0
            if confidence < confidence_threshold:
                continue
            if wrong not in transcript:
                continue
            seen_wrong.add(wrong)
            new_corrections.append({
                "wrong": wrong,
                "correct": correct,
                "confidence": round(confidence, 3),
                "reason": str(item.get("reason", "")).strip(),
            })

    return {"new_glossary": new_glossary, "new_corrections": new_corrections}

