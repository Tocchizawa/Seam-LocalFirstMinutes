from __future__ import annotations

import gc
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query

from src.api.errors import forbidden
from src.api.recording import get_runtime_debug_snapshot
from src.config import config

router = APIRouter(prefix="/api/debug", tags=["debug"])


def _read_log_tail(lines: int) -> list[str]:
    log_dir = Path(config.get("logging", "dir", default="~/.seam/logs/")).expanduser()
    log_file = log_dir / "seam.log"
    if not log_file.exists():
        return []
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=lines)]
    except Exception:
        return []


@router.get("/status")
async def debug_status(lines: int = Query(default=120, ge=20, le=500)) -> dict:
    if not config.get("debug", "enabled", default=False):
        raise forbidden("DEBUG_DISABLED", "debug API is disabled")

    process_info: dict = {}
    system_info: dict = {}

    try:
        import psutil  # type: ignore

        proc = psutil.Process()
        pmem = proc.memory_info()
        cpu = proc.cpu_percent(interval=None)
        system_mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        process_info = {
            "pid": proc.pid,
            "cpu_percent": cpu,
            "rss_mb": round(pmem.rss / (1024 ** 2), 1),
            "vms_mb": round(pmem.vms / (1024 ** 2), 1),
            "threads": proc.num_threads(),
            "open_files": len(proc.open_files()),
        }
        system_info = {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_total_gb": round(system_mem.total / (1024 ** 3), 1),
            "memory_used_gb": round(system_mem.used / (1024 ** 3), 1),
            "memory_percent": system_mem.percent,
            "swap_percent": swap.percent,
        }
    except Exception:
        pass

    return {
        "debug_enabled": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": get_runtime_debug_snapshot(),
        "process": process_info,
        "system": system_info,
        "gc": {
            "counts": list(gc.get_count()),
            "threshold": list(gc.get_threshold()),
        },
        "log_tail": _read_log_tail(lines),
    }
