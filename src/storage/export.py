from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _fmt_duration_ja(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}時間{m}分" if h > 0 else f"{m}分"


def _fmt_started_jp(started_at: str) -> str:
    try:
        dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        return dt.strftime("%Y年%-m月%-d日 %H:%M")
    except (ValueError, AttributeError):
        return started_at


def to_markdown(minutes: dict, project_name: str | None = None) -> str:
    """議事録 dict から Markdown を生成する。"""
    title = minutes.get("title", "議事録")
    date = minutes.get("date", "")
    duration = int(minutes.get("duration_sec", 0))
    started_at = minutes.get("started_at", "")
    summary = (minutes.get("summary") or "").strip()
    transcript = minutes.get("transcript") or []

    lines: list[str] = []
    header = f"# 議事録: {project_name} - {date}" if project_name else f"# {title}"
    lines.append(header)
    lines.append("")

    meta: list[str] = []
    if started_at:
        meta.append(f"**日時**: {_fmt_started_jp(started_at)}")
    if duration > 0:
        meta.append(f"**時間**: {_fmt_duration_ja(duration)}")
    if project_name:
        meta.append(f"**プロジェクト**: {project_name}")
    if meta:
        lines.extend(meta)
        lines.append("")

    lines.append("---")
    lines.append("")

    if summary:
        lines.append("## 要約")
        lines.append("")
        lines.append(summary)
        lines.append("")

    if transcript:
        lines.append("## 全文書き起こし")
        lines.append("")
        for seg in transcript:
            ts = _fmt_ts(float(seg.get("start", 0)))
            text = (seg.get("text") or "").strip()
            if text:
                speaker = (seg.get("speaker_label") or "").strip()
                prefix = f"[{ts}]"
                if speaker:
                    prefix = f"{prefix} ({speaker})"
                lines.append(f"{prefix} {text}")
                lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _filename(minutes: dict) -> str:
    """``YYYY-MM-DD-<タイトル>.md`` 形式。

    タイトルが空 / 既定 (XX:XX の会議) の場合は session id 末尾でフォールバック。
    ファイル名に使えない文字 (/ \\ : * ? " < > |) は '_' に置換。
    """
    started = minutes.get("started_at", "")
    try:
        dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        date_str = (minutes.get("date") or "unknown").replace("/", "-")

    title = str(minutes.get("title") or "").strip()
    # 既定の自動命名 ("HH:MM の会議") は実質意味が無いので fallback
    is_default_auto_name = bool(re.fullmatch(r"\d{1,2}:\d{2} の会議", title))
    if not title or is_default_auto_name:
        session_id = minutes.get("session_id", "")
        short = session_id[-6:] if session_id else "unknown"
        slug = f"会議-{short}"
    else:
        # ファイル名に不正な文字を除去
        slug = re.sub(r'[\\/:*?"<>|]', "_", title)
        # 空白の連続をハイフンに
        slug = re.sub(r"\s+", "-", slug).strip("-")
        # 長すぎる場合は60文字でカット
        if len(slug) > 60:
            slug = slug[:60].rstrip("-")
    return f"{date_str}-{slug}.md"


def export_to_dir(minutes: dict, output_dir: str | Path,
                  project_name: str | None = None) -> Path:
    """Markdown を output_dir に書き出してパスを返す。"""
    out = Path(output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    path = out / _filename(minutes)
    path.write_text(to_markdown(minutes, project_name), encoding="utf-8")
    logger.info("Exported minutes Markdown: %s", path)
    return path
