"""Phase B (要約後の辞書整理): transcript + docs + 既存辞書を LLM に渡し、
新規 glossary 用語と新規 corrections ペアを取得する。
"""
from __future__ import annotations

import json
import logging
import os
from typing import TypedDict

from src.summarize.base import SummaryError

from .extractor import CLI_PROVIDERS, _call_llm_for_dictionary, collect_docs
from .prompts import (
    DICTIONARY_UPDATE_SYSTEM_PROMPT,
    build_dictionary_update_user_prompt_agentic,
    build_dictionary_update_user_prompt_inline,
    extract_json_object,
)

logger = logging.getLogger(__name__)

# transcript が長すぎると LLM の context を圧迫するため、先頭+末尾を抽出
MAX_TRANSCRIPT_CHARS = 12000
HEAD_CHARS = 8000
TAIL_CHARS = 3000

# API provider 向けの docs 抜粋上限 (CLI は inline しないので無関係)
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


def _resolve_cli_paths(doc_dirs: list[str] | None, repo_path: str | None) -> list[str]:
    paths: list[str] = []
    if repo_path and os.path.isdir(repo_path):
        paths.append(repo_path)
    for d in doc_dirs or []:
        if d and os.path.isdir(d) and d not in paths:
            paths.append(d)
    return paths


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
) -> DictionaryUpdate:
    """Phase B: 直近の transcript と docs を照合し、glossary 追加候補 + corrections 追加候補を返す。

    CLI providers (claude_code/codex) は --add-dir で docs を自由探索。
    API providers は docs を inline (上限あり)。
    """
    if not transcript or not transcript.strip():
        return {"new_glossary": [], "new_corrections": []}

    is_cli = provider_name in CLI_PROVIDERS
    cli_paths = _resolve_cli_paths(doc_dirs, repo_path) if is_cli else []
    truncated = _truncate_transcript(transcript)

    if is_cli:
        if not cli_paths and not existing_glossary:
            # docs にもアクセスできず既存 glossary もない → 抽出材料不足
            return {"new_glossary": [], "new_corrections": []}
        user_p = build_dictionary_update_user_prompt_agentic(
            truncated,
            project_name=project_name,
            doc_paths=cli_paths,
            existing_glossary=existing_glossary,
            existing_corrections=existing_corrections,
        )
        # CLI は深く読むので長めに
        if timeout_sec < 600:
            timeout_sec = 600.0
    else:
        docs_text = ""
        if doc_dirs or repo_path:
            docs_text = collect_docs(doc_dirs or [], repo_path)[:MAX_DOC_CHARS_FOR_CORRECTION]
        if not existing_glossary and not docs_text:
            return {"new_glossary": [], "new_corrections": []}
        user_p = build_dictionary_update_user_prompt_inline(
            truncated,
            project_name=project_name,
            docs_text=docs_text,
            existing_glossary=existing_glossary,
            existing_corrections=existing_corrections,
        )

    try:
        text = await _call_llm_for_dictionary(
            provider_name=provider_name,
            ai_cfg=ai_cfg,
            system_prompt=DICTIONARY_UPDATE_SYSTEM_PROMPT,
            user_prompt=user_p,
            timeout_sec=timeout_sec,
            max_tokens=2500,
            cli_paths=cli_paths,
        )
    except SummaryError as e:
        logger.warning("[phase-b] LLM call failed: %s", e.message)
        return {"new_glossary": [], "new_corrections": []}

    json_str = extract_json_object(text)
    if not json_str:
        logger.warning("[phase-b] no JSON in LLM output: %s", text[:200])
        return {"new_glossary": [], "new_corrections": []}
    try:
        obj = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning("[phase-b] JSON parse failed: %s", e)
        return {"new_glossary": [], "new_corrections": []}

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

