from __future__ import annotations

from typing import Any

import numpy as np


DEFAULT_AUDIO_LEVELING: dict[str, Any] = {
    "enabled": True,
    "realtime_enabled": True,
    "final_normalize": True,
    "target_rms": 0.08,
    "noise_floor": 0.003,
    "max_gain": 12.0,
    "attack": 0.18,
    "release": 0.55,
    "peak_limit": 0.95,
    "frame_ms": 100,
    "gauss_size": 3,
}


def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except Exception:
        v = default
    return max(lo, min(hi, v))


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except Exception:
        v = default
    return max(lo, min(hi, v))


def normalize_audio_leveling_config(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    cfg = dict(DEFAULT_AUDIO_LEVELING)
    cfg.update(src)
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["realtime_enabled"] = bool(cfg.get("realtime_enabled", True))
    cfg["final_normalize"] = bool(cfg.get("final_normalize", True))
    cfg["target_rms"] = _clamp_float(cfg.get("target_rms"), 0.08, 0.01, 0.30)
    cfg["noise_floor"] = _clamp_float(cfg.get("noise_floor"), 0.003, 0.0001, 0.05)
    cfg["max_gain"] = _clamp_float(cfg.get("max_gain"), 12.0, 1.0, 20.0)
    cfg["attack"] = _clamp_float(cfg.get("attack"), 0.18, 0.01, 1.0)
    cfg["release"] = _clamp_float(cfg.get("release"), 0.55, 0.01, 1.0)
    cfg["peak_limit"] = _clamp_float(cfg.get("peak_limit"), 0.95, 0.50, 1.0)
    cfg["frame_ms"] = _clamp_int(cfg.get("frame_ms"), 100, 50, 5000)
    gauss_size = _clamp_int(cfg.get("gauss_size"), 3, 3, 301)
    if gauss_size % 2 == 0:
        gauss_size += 1
    cfg["gauss_size"] = min(301, gauss_size)
    return cfg


def build_ffmpeg_loudness_filter(raw: Any) -> str | None:
    cfg = normalize_audio_leveling_config(raw)
    if not cfg["enabled"] or not cfg["final_normalize"]:
        return None
    return (
        "dynaudnorm="
        f"f={cfg['frame_ms']}:"
        f"g={cfg['gauss_size']}:"
        f"p={cfg['peak_limit']:.3f}:"
        f"m={cfg['max_gain']:.2f}:"
        f"r={cfg['target_rms']:.3f}:"
        f"t={cfg['noise_floor']:.4f}:"
        "n=1,"
        f"alimiter=limit={cfg['peak_limit']:.3f}:attack=5:release=80"
    )


class AdaptiveSpeechGain:
    """Boost low speech frames without raising near-silence noise floors."""

    def __init__(
        self,
        *,
        sample_rate: int,
        target_rms: float,
        noise_floor: float,
        max_gain: float,
        attack: float,
        release: float,
        peak_limit: float,
        block_ms: int = 100,
    ) -> None:
        self.sample_rate = max(8000, int(sample_rate))
        self.target_rms = float(target_rms)
        self.noise_floor = float(noise_floor)
        self.max_gain = float(max_gain)
        self.attack = float(attack)
        self.release = float(release)
        self.peak_limit = float(peak_limit)
        self.block_size = max(1, int(self.sample_rate * max(20, block_ms) / 1000))
        self._gain = 1.0
        self.last_gain = 1.0
        self.last_rms = 0.0

    @classmethod
    def from_config(cls, raw: Any, *, sample_rate: int) -> AdaptiveSpeechGain | None:
        cfg = normalize_audio_leveling_config(raw)
        if not cfg["enabled"] or not cfg["realtime_enabled"]:
            return None
        return cls(
            sample_rate=sample_rate,
            target_rms=cfg["target_rms"],
            noise_floor=cfg["noise_floor"],
            max_gain=cfg["max_gain"],
            attack=cfg["attack"],
            release=cfg["release"],
            peak_limit=cfg["peak_limit"],
        )

    def process(self, samples: np.ndarray) -> np.ndarray:
        arr = np.asarray(samples, dtype=np.float32)
        if arr.size == 0:
            return arr

        out = np.empty_like(arr, dtype=np.float32)
        for start in range(0, len(arr), self.block_size):
            block = arr[start:start + self.block_size]
            if block.size == 0:
                continue

            rms = float(np.sqrt(np.mean(block * block)))
            peak = float(np.max(np.abs(block))) if block.size else 0.0
            self.last_rms = rms

            if rms < self.noise_floor:
                desired = 1.0
            else:
                desired = self.target_rms / max(rms, 1e-8)
                desired = max(1.0, min(self.max_gain, desired))
                if peak > 1e-8:
                    desired = min(desired, self.peak_limit / peak)

            coeff = self.attack if desired > self._gain else self.release
            self._gain += (desired - self._gain) * coeff
            self.last_gain = self._gain

            out[start:start + self.block_size] = np.clip(
                block * self._gain,
                -self.peak_limit,
                self.peak_limit,
            )
        return out
