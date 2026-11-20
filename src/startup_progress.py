"""Backend の起動進捗 emit ユーティリティ。

stderr に ``[seam-progress] {json}`` 形式で 1 行出力する。

JSON のキー:
  phase    : 任意ラベル ("models" / "recovery" / "summary" / "ready" 等)
  message  : 表示用テキスト (例: "バックエンドを準備中")
  progress : 0.0-1.0 (None なら不確定スピナー)
  detail   : 追加詳細 (省略可)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def emit(
    phase: str,
    message: str,
    progress: float | None = None,
    detail: str | None = None,
) -> None:
    payload: dict[str, object] = {"phase": phase, "message": message}
    if progress is not None:
        payload["progress"] = max(0.0, min(1.0, float(progress)))
    if detail:
        payload["detail"] = detail
    encoded = json.dumps(payload, ensure_ascii=False)
    try:
        print("[seam-progress] " + encoded, file=sys.stderr, flush=True)
    except Exception:
        # 進捗 emit でアプリ起動を止めない
        logger.debug("startup progress emit failed", exc_info=True)

    if target := os.environ.get("SEAM_DESKTOP_PROGRESS"):
        try:
            path = Path(target)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            logger.debug("desktop startup progress write failed", exc_info=True)
