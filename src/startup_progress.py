"""Backend → Splash の起動進捗 emit ユーティリティ。

stderr に ``[seam-progress] {json}`` 形式で 1 行出力し、Rust 側
(`gui/src-tauri/src/lib.rs`) で `parse_startup_line` がパースする。

JSON のキー:
  phase    : 任意ラベル ("models" / "recovery" / "summary" / "ready" 等)
  message  : 表示用テキスト (例: "話者分離モデルをロード中")
  progress : 0.0-1.0 (None なら不確定スピナー)
  detail   : 追加詳細 (省略可)
"""
from __future__ import annotations

import json
import logging
import sys

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
    try:
        line = "[seam-progress] " + json.dumps(payload, ensure_ascii=False)
        print(line, file=sys.stderr, flush=True)
    except Exception:
        # 進捗 emit でアプリ起動を止めない
        logger.debug("startup progress emit failed", exc_info=True)
