from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.audio.resource_monitor import ModelResourceGate, ResourceMonitor


class ResourceMonitorTest(unittest.TestCase):
    @staticmethod
    def _memory(percent: float) -> SimpleNamespace:
        total = 16 * 1024 ** 3
        return SimpleNamespace(
            total=total,
            used=int(total * percent / 100),
            available=int(total * (1 - percent / 100)),
            percent=percent,
        )

    @patch("src.audio.resource_monitor.psutil.virtual_memory")
    @patch("src.audio.resource_monitor.psutil.cpu_percent", return_value=20.0)
    def test_memory_pressure_requests_throttle(self, _cpu, virtual_memory) -> None:
        virtual_memory.return_value = self._memory(90.0)
        monitor = ResourceMonitor()

        self.assertTrue(
            monitor.should_throttle(cpu_threshold=75.0, memory_threshold=85.0)
        )

    @patch("src.audio.resource_monitor.psutil.virtual_memory")
    @patch("src.audio.resource_monitor.psutil.cpu_percent", return_value=20.0)
    def test_normal_load_does_not_request_throttle(self, _cpu, virtual_memory) -> None:
        virtual_memory.return_value = self._memory(40.0)
        monitor = ResourceMonitor()

        self.assertFalse(
            monitor.should_throttle(cpu_threshold=75.0, memory_threshold=85.0)
        )

    def test_priority_access_denied_does_not_break_transcription(self) -> None:
        monitor = ResourceMonitor()
        monitor._process = SimpleNamespace(
            nice=Mock(side_effect=psutil.AccessDenied(pid=1234))
        )

        self.assertFalse(monitor.apply_process_priority(3))


class ModelResourceGateTest(unittest.TestCase):
    def test_llm_waits_until_whisper_is_released(self) -> None:
        gate = ModelResourceGate()
        self.assertTrue(gate.try_acquire_whisper())

        async def scenario() -> None:
            acquired = asyncio.Event()

            async def acquire_llm() -> None:
                await gate.acquire_llm_async()
                acquired.set()

            waiter = asyncio.create_task(acquire_llm())
            await asyncio.sleep(0.02)
            self.assertFalse(acquired.is_set())

            gate.release_whisper()
            await asyncio.wait_for(acquired.wait(), timeout=1.0)
            self.assertTrue(gate.snapshot()["llm_active"])
            self.assertFalse(gate.try_acquire_whisper())
            gate.release_llm()
            await asyncio.wait_for(waiter, timeout=1.0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
