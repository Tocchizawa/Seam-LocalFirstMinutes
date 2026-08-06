"""システム負荷の監視と、重いローカルモデルの排他制御。"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable

import psutil

logger = logging.getLogger(__name__)

_CPU_COUNT = os.cpu_count() or 4
_GIB = 1024 ** 3
_MIB = 1024 ** 2


class ResourceBusyError(RuntimeError):
    """ローカルの重いモデルを別処理が使用中。"""


class ModelResourceGate:
    """Whisper と LLM を同時に動かさないためのプロセス内ゲート。

    Whisper は録音中に複数セッションから共有され得るため複数取得を許可し、
    LLM は Whisper がすべて解放されるまで待機する。録音開始は LLM 実行中だけ
    拒否して、録音音声をキューに溜め続ける状態を作らない。
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._whisper_users = 0
        self._llm_waiters = 0
        self._llm_active = False

    def try_acquire_whisper(self) -> bool:
        """Whisper の使用権を即時取得する。LLM 実行中は False。"""
        with self._condition:
            if self._llm_active:
                return False
            self._whisper_users += 1
            return True

    def release_whisper(self, before_release: Callable[[], None] | None = None) -> None:
        with self._condition:
            if self._whisper_users == 1 and before_release is not None:
                try:
                    before_release()
                except Exception as exc:
                    logger.warning("Whisper cleanup before lease release failed: %s", exc)
            self._whisper_users = max(0, self._whisper_users - 1)
            self._condition.notify_all()

    def try_acquire_llm(self) -> bool:
        with self._condition:
            if self._llm_active or self._whisper_users:
                return False
            self._llm_active = True
            return True

    async def acquire_llm_async(self) -> None:
        """キャンセル可能な async 版の LLM 使用権取得。"""
        with self._condition:
            self._llm_waiters += 1
        try:
            while not self.try_acquire_llm():
                await asyncio.sleep(0.25)
        finally:
            with self._condition:
                self._llm_waiters -= 1

    def release_llm(self) -> None:
        with self._condition:
            self._llm_active = False
            self._condition.notify_all()

    def snapshot(self) -> dict:
        with self._condition:
            return {
                "whisper_users": self._whisper_users,
                "llm_waiters": self._llm_waiters,
                "llm_active": self._llm_active,
            }


model_resource_gate = ModelResourceGate()


class ResourceMonitor:
    """CPU・メモリ負荷を読み取り、処理を抑制すべきか判断する。"""

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
        try:
            self._process = psutil.Process()
        except Exception:
            self._process = None

    def _recommend_threads(self, cpu_pct: float, mem_pct: float) -> int:
        if mem_pct >= self.mem_critical or cpu_pct >= self.cpu_critical:
            return self._min_threads
        if cpu_pct <= self.cpu_high:
            return self._max_threads

        # cpu_high〜cpu_critical の間で線形補間
        span = max(1.0, self.cpu_critical - self.cpu_high)
        ratio = (cpu_pct - self.cpu_high) / span
        threads = self._max_threads - ratio * (self._max_threads - self._min_threads)
        return max(self._min_threads, round(threads))

    def _read(self, *, cpu_interval: float | None = None) -> dict:
        cpu_pct = psutil.cpu_percent(interval=cpu_interval)
        mem = psutil.virtual_memory()
        result = {
            "cpu_count": _CPU_COUNT,
            "cpu_percent": round(float(cpu_pct), 1),
            "mem_total_gb": round(mem.total / _GIB, 1),
            "mem_used_gb": round(mem.used / _GIB, 1),
            "mem_available_gb": round(mem.available / _GIB, 1),
            "mem_percent": round(float(mem.percent), 1),
        }
        if self._process is not None:
            try:
                process_mem = self._process.memory_info()
                result.update({
                    "process_rss_mb": round(process_mem.rss / _MIB, 1),
                    "process_vms_mb": round(process_mem.vms / _MIB, 1),
                    "process_memory_percent": round(
                        process_mem.rss / max(1, mem.total) * 100, 2
                    ),
                })
            except Exception:
                pass
        return result

    def should_throttle(
        self,
        *,
        cpu_threshold: float,
        memory_threshold: float = 85.0,
    ) -> bool:
        """現在の CPU またはシステムメモリが閾値以上かを返す。"""
        return self.pressure(
            cpu_threshold=cpu_threshold,
            memory_threshold=memory_threshold,
        )[0]

    def pressure(
        self,
        *,
        cpu_threshold: float,
        memory_threshold: float = 85.0,
    ) -> tuple[bool, tuple[str, ...]]:
        """負荷状態と、該当した閾値 (cpu / memory) を返す。"""
        current = self._read(cpu_interval=None)
        pressures: list[str] = []
        if current["cpu_percent"] >= float(cpu_threshold):
            pressures.append("cpu")
        if current["mem_percent"] >= float(memory_threshold):
            pressures.append("memory")
        return bool(pressures), tuple(pressures)

    def recommend_threads(self) -> int:
        """現在のシステム負荷から推奨スレッド数を返す。"""
        current = self._read(cpu_interval=0.5)
        threads = self._recommend_threads(current["cpu_percent"], current["mem_percent"])
        logger.info(
            "Resource check: CPU=%.0f%% MEM=%.0f%% → threads=%d (cores=%d)",
            current["cpu_percent"], current["mem_percent"], threads, _CPU_COUNT,
        )
        return threads

    def snapshot(self) -> dict:
        """現在のシステム状況と Seam プロセスの使用量をまとめて返す。"""
        current = self._read(cpu_interval=None)
        current["recommended_threads"] = self._recommend_threads(
            current["cpu_percent"], current["mem_percent"]
        )
        return current

    def apply_process_priority(self, nice_value: int) -> bool:
        """Seam バックエンドの優先度を設定する。

        macOS の Python ではスレッド単位の nice 設定ができないため、これは
        Whisper 専用ではなくバックエンドプロセス全体に適用する。
        """
        try:
            value = max(0, min(19, int(nice_value)))
            process = self._process or psutil.Process()
            process.nice(value)
            return True
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            logger.debug("Process priority could not be set to %s: %s", nice_value, exc)
            return False
