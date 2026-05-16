"""システムリソース監視モジュール

CPU 使用率・メモリ使用率をもとに Whisper の計算資源（スレッド数）を
動的に調整するための推奨値を返す。
"""
from __future__ import annotations

import logging
import os

import psutil

logger = logging.getLogger(__name__)

_CPU_COUNT = os.cpu_count() or 4


class ResourceMonitor:
    """システム負荷に応じて Whisper 用の推奨スレッド数を返す。

    Parameters
    ----------
    cpu_high : float
        CPU 使用率がこの閾値を超えたらスレッド数を減らし始める (0-100)。
    cpu_critical : float
        CPU 使用率がこの閾値を超えたら最小スレッド数に落とす (0-100)。
    mem_critical : float
        メモリ使用率がこの閾値を超えたらスレッド数を最小に落とす (0-100)。
    """

    def __init__(
        self,
        cpu_high: float = 50.0,
        cpu_critical: float = 80.0,
        mem_critical: float = 85.0,
    ) -> None:
        self.cpu_high = cpu_high
        self.cpu_critical = cpu_critical
        self.mem_critical = mem_critical
        self._max_threads = max(1, _CPU_COUNT - 2)  # 他プロセス用に 2 コア残す
        self._min_threads = max(1, _CPU_COUNT // 4)

    def recommend_threads(self) -> int:
        """現在のシステム負荷から推奨スレッド数を返す。"""
        cpu_pct = psutil.cpu_percent(interval=0.5)
        mem_pct = psutil.virtual_memory().percent

        if mem_pct >= self.mem_critical:
            threads = self._min_threads
        elif cpu_pct >= self.cpu_critical:
            threads = self._min_threads
        elif cpu_pct <= self.cpu_high:
            threads = self._max_threads
        else:
            # cpu_high〜cpu_critical の間で線形補間
            ratio = (cpu_pct - self.cpu_high) / (self.cpu_critical - self.cpu_high)
            threads = self._max_threads - ratio * (self._max_threads - self._min_threads)
            threads = max(self._min_threads, round(threads))

        logger.info(
            "Resource check: CPU=%.0f%% MEM=%.0f%% → threads=%d (cores=%d)",
            cpu_pct, mem_pct, threads, _CPU_COUNT,
        )
        return threads

    def snapshot(self) -> dict:
        """現在のシステム状況をまとめて返す。"""
        cpu_pct = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        return {
            "cpu_count": _CPU_COUNT,
            "cpu_percent": cpu_pct,
            "mem_total_gb": round(mem.total / (1024 ** 3), 1),
            "mem_percent": mem.percent,
            "recommended_threads": self.recommend_threads(),
        }
