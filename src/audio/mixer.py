"""録音中のマイク + システム音声をリアルタイムにミックス。

- mic: 任意 native rate, int16 → 16kHz float32 にリサンプル
- system: 48kHz float32 → 16kHz float32 にリサンプル
- 両方の最小長さ分を mix(クリップ)し、コンシューマーに渡す
- システムキャプチャ無しの場合は mic のみそのまま渡す
"""
from __future__ import annotations

import logging
import threading
import time
from math import gcd
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)


SAMPLE_RATE = 16000
BLOCK_MS = 100
MAX_BUFFER_SEC = 5


class RealtimeMixer:
    """ミックス済み 16kHz float32 mono サンプルを on_chunk に渡す。"""

    def __init__(self, on_chunk: Callable[[np.ndarray], None] | None = None) -> None:
        self._on_chunk = on_chunk
        self._block_size = int(SAMPLE_RATE * BLOCK_MS / 1000)
        self._max_buf = int(SAMPLE_RATE * MAX_BUFFER_SEC)
        self._mic_buf = np.zeros(0, dtype=np.float32)
        self._sys_buf = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._consumer: threading.Thread | None = None
        self._consumer_generation = 0
        self._running = False
        self._has_system = False
        self._system_seen = False
        self._mic_seen = False
        self._consumer_restarts = 0
        self._last_restart_reason: str | None = None
        self._last_restart_at: float = 0.0

    def start(self, has_system: bool = False) -> None:
        self._running = True
        self._has_system = has_system
        self._system_seen = False
        self._mic_seen = False
        with self._lock:
            self._mic_buf = np.zeros(0, dtype=np.float32)
            self._sys_buf = np.zeros(0, dtype=np.float32)
        self._consumer_generation += 1
        self._consumer = threading.Thread(
            target=self._consume,
            args=(self._consumer_generation,),
            daemon=True,
            name=f"realtime-mixer-{self._consumer_generation}",
        )
        self._consumer.start()

    def stop(self) -> None:
        with self._cond:
            self._running = False
            self._cond.notify_all()
        if self._consumer:
            self._consumer.join(timeout=2)
            self._consumer = None
        self._flush_remaining()

    @property
    def consumer_alive(self) -> bool:
        return self._consumer is not None and self._consumer.is_alive()

    @property
    def consumer_restarts(self) -> int:
        return self._consumer_restarts

    def restart_consumer(self, reason: str) -> bool:
        if not self._running:
            return False
        if self.consumer_alive:
            return False
        self._consumer_generation += 1
        self._consumer = threading.Thread(
            target=self._consume,
            args=(self._consumer_generation,),
            daemon=True,
            name=f"realtime-mixer-{self._consumer_generation}",
        )
        self._consumer.start()
        self._consumer_restarts += 1
        self._last_restart_reason = reason
        self._last_restart_at = time.monotonic()
        logger.warning("Mixer consumer restarted (reason=%s, gen=%d)", reason, self._consumer_generation)
        return True

    def get_debug_snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            mic_buf_sec = round(len(self._mic_buf) / SAMPLE_RATE, 2)
            sys_buf_sec = round(len(self._sys_buf) / SAMPLE_RATE, 2)
            system_seen = self._system_seen
        return {
            "consumer_alive": self.consumer_alive,
            "consumer_restarts": self._consumer_restarts,
            "last_restart_reason": self._last_restart_reason,
            "last_restart_age_sec": round(now - self._last_restart_at, 2) if self._last_restart_at > 0 else None,
            "has_system": self._has_system,
            "system_seen": system_seen,
            "mic_buffer_sec": mic_buf_sec,
            "system_buffer_sec": sys_buf_sec,
        }

    # ─── feeds ───────────────────────────────────────────────

    def feed_mic(self, samples_int16: np.ndarray, sample_rate: int) -> None:
        if not self._running or samples_int16 is None or len(samples_int16) == 0:
            return
        try:
            f32 = (samples_int16.astype(np.float32) / 32768.0)
            f32 = self._resample(f32, sample_rate)
        except Exception as e:
            logger.error("Mixer mic resample error: %s", e)
            return
        with self._cond:
            self._mic_buf = np.concatenate([self._mic_buf, f32])
            if len(self._mic_buf) > self._max_buf:
                self._mic_buf = self._mic_buf[-self._max_buf:]
            self._mic_seen = True
            self._cond.notify()

    def feed_system(self, samples_f32: np.ndarray, sample_rate: int = 48000) -> None:
        if not self._running or samples_f32 is None or len(samples_f32) == 0:
            return
        try:
            f32 = self._resample(samples_f32.astype(np.float32), sample_rate)
        except Exception as e:
            logger.error("Mixer system resample error: %s", e)
            return
        with self._cond:
            self._sys_buf = np.concatenate([self._sys_buf, f32])
            if len(self._sys_buf) > self._max_buf:
                self._sys_buf = self._sys_buf[-self._max_buf:]
            self._system_seen = True
            self._cond.notify()

    # ─── consumer ────────────────────────────────────────────

    def _consume(self, generation: int) -> None:
        try:
            import os
            os.nice(5)
        except (OSError, AttributeError):
            pass
        import time as _time
        while True:
            if generation != self._consumer_generation:
                logger.info("Mixer consumer generation changed, exiting old worker (gen=%d)", generation)
                return
            try:
                with self._cond:
                    while self._running:
                        mic_n = len(self._mic_buf)
                        sys_n = len(self._sys_buf)
                        if self._has_system:
                            # タイムライン基準は mic 側に固定する。
                            # system が遅延/欠落しても mic の時間を止めず、欠損ぶんは無音で埋める。
                            if mic_n >= self._block_size:
                                mic = self._mic_buf[:self._block_size].copy()
                                self._mic_buf = self._mic_buf[self._block_size:]
                                sys = self._take_with_silence("_sys_buf", self._block_size)
                                mixed = self._mix(mic, sys)
                                break
                            # mic が一度も来ていない特殊ケースでは system 単体も通す。
                            if not self._mic_seen and sys_n >= self._block_size:
                                sys = self._sys_buf[:self._block_size].copy()
                                self._sys_buf = self._sys_buf[self._block_size:]
                                mixed = sys
                                break
                        else:
                            if mic_n >= self._block_size:
                                mic = self._mic_buf[:self._block_size].copy()
                                self._mic_buf = self._mic_buf[self._block_size:]
                                mixed = mic
                                break
                        self._cond.wait(timeout=0.5)
                    else:
                        return
                self._dispatch(mixed)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error("Mixer consumer error (will continue): %s", e, exc_info=True)
                _time.sleep(0.1)
                if not self._running:
                    return

    def _flush_remaining(self) -> None:
        with self._lock:
            if self._has_system:
                n = len(self._mic_buf)
                if n > 0:
                    mic = self._mic_buf.copy()
                    sys = np.zeros(n, dtype=np.float32)
                    if len(self._sys_buf) > 0:
                        take = min(n, len(self._sys_buf))
                        sys[:take] = self._sys_buf[:take]
                    mixed = self._mix(mic, sys)
                elif not self._mic_seen and len(self._sys_buf) > 0:
                    mixed = self._sys_buf.copy()
                else:
                    mixed = None
            else:
                mixed = self._mic_buf if len(self._mic_buf) > 0 else None
            self._mic_buf = np.zeros(0, dtype=np.float32)
            self._sys_buf = np.zeros(0, dtype=np.float32)
        if mixed is not None and len(mixed) > 0:
            self._dispatch(mixed)

    def _dispatch(self, samples: np.ndarray) -> None:
        if self._on_chunk is None:
            return
        try:
            self._on_chunk(samples)
        except Exception as e:
            logger.error("Mixer on_chunk error: %s", e)

    # ─── helpers ─────────────────────────────────────────────

    @staticmethod
    def _mix(mic: np.ndarray, sys: np.ndarray) -> np.ndarray:
        return np.clip(mic + sys, -1.0, 1.0).astype(np.float32)

    def _take_with_silence(self, attr: str, n: int) -> np.ndarray:
        buf = getattr(self, attr)
        out = np.zeros(n, dtype=np.float32)
        if len(buf) > 0:
            take = min(n, len(buf))
            out[:take] = buf[:take]
            setattr(self, attr, buf[take:])
        return out

    @staticmethod
    def _resample(samples: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate == SAMPLE_RATE:
            return samples
        from scipy.signal import resample_poly
        g = gcd(SAMPLE_RATE, sample_rate)
        up = SAMPLE_RATE // g
        down = sample_rate // g
        out = resample_poly(samples, up, down)
        return out.astype(np.float32)
