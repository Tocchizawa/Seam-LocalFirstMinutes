from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

APP_DIR = Path("~/.seam").expanduser()

DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "ollama": {
        # context expansion agent 用 (要約とは別 feature)
        "base_url": "http://localhost:11434",
        "context_model": "qwen3:8b",
        "auto_start": True,
        "auto_install": True,
    },
    "minutes_ai": {
        "provider": "ollama",         # ollama | claude_api | openai | gemini
        "auto_generate": True,        # 録音停止時に自動要約 (provider未設定なら skip)
        "generate_title": True,       # 要約時に議事録タイトルも自動生成して上書き
        "auto_dictionary_update": True,  # 要約後に project.glossary / corrections を自動更新 (Phase B)
        "correction_confidence": 0.85,   # Phase B での誤転写ペア採用 confidence 閾値
        "timeout_sec": 300,
        "pull_timeout_sec": 1800,
        # provider 初使用時の同意フラグ (cloud のみ意味あり)
        "consent": {
            "claude_api": False,
            "openai": False,
            "gemini": False,
        },
        "ollama": {
            "base_url": "http://localhost:11434",
            "model": "qwen3:8b",       # qwen3:4b | qwen3:8b | qwen3:14b | qwen3:32b
            "num_ctx": 8192,
            "num_thread": 1,
            "num_batch": 8,
            # -1 = 全レイヤーGPU (Apple Silicon Metalで最速)。0 = CPUのみ。
            # 要約は録音停止後なのでGPU使用を許可するのが既定。
            "num_gpu": -1,
            "low_vram": False,
            "dynamic_budget_enabled": True,
            "spare_usage_ratio": 0.6,
            "keep_alive_sec": 0,
        },
        "claude_api": {
            "model": "claude-sonnet-4-6",
            "max_tokens": 8192,
            "use_prompt_caching": True,
        },
        "openai": {
            "model": "gpt-4o-mini",
            "max_tokens": 8192,
        },
        "gemini": {
            "model": "gemini-2.0-flash",
            "max_tokens": 8192,
        },
        "claude_code": {
            "binary_path": "claude",
            "model": "sonnet",   # alias OK (sonnet | opus | haiku) or full ID
            # zsh function 等の特殊起動が必要な場合だけ使用。
            # 例: "source ~/.zshrc; my_claude_wrapper"
            "launcher_command": "",
            "launcher_shell": "/bin/zsh",
            "launcher_interactive": True,
            "extra_args": [],
        },
        "codex": {
            "binary_path": "codex",
            # codex CLI のモデル一覧は ~/.codex/models_cache.json から動的取得 (UI で選択)。
            # ここの default はフォールバック (キャッシュ未取得時)。空文字なら --model を付けない。
            "model": "",
            # zsh function 等の特殊起動が必要な場合だけ使用。
            # 例: "source ~/.zshrc; my_codex_wrapper"
            "launcher_command": "",
            "launcher_shell": "/bin/zsh",
            "launcher_interactive": True,
            "extra_args": [],
        },
    },
    "whisper": {
        "model": "medium",
        "language": "ja",
        "device": "auto",
        # 起動直後の常駐メモリ増加を避けるため、既定は遅延ロード。
        "preload_on_startup": False,
        "streaming": {
            # VAD 種類: "silero" (推奨, ノイズ耐性高) or "energy" (フォールバック, 軽量)
            "vad_provider": "silero",
            "silero_threshold": 0.5,
            "rms_threshold": 0.005,
            "silence_duration_ms": 500,
            "min_chunk_ms": 1000,
            "max_chunk_ms": 12000,
            # 0 は queue.Queue の無制限モード
            "max_queue_chunks": 0,
            "max_pending_audio_sec": 240.0,
            "model_load_timeout_sec": 900.0,
            "flush_join_timeout_sec": 120.0,
        },
        "hallucination_filter": {
            "enabled": True,
            "max_direct_match_len": 64,
            "blocked_phrases": [],
        },
        # 話者記憶: 全プロジェクト共通で扱う。
        "speaker_memory": {
            "enabled": True,
            "match_threshold": 0.82,
            "min_audio_sec": 1.0,
            "sample_sec": 2.5,
            "global_match_threshold": 0.992,
            "rediarize_keep_ratio": 0.86,
            # 話者分離プロバイダ: "legacy" (従来の手作り特徴量) or "pyannote"
            "diarization_provider": "legacy",
            "pyannote_model": "pyannote/speaker-diarization-3.1",
            "pyannote_device": "auto",
            "pyannote_min_speakers": None,
            "pyannote_max_speakers": None,
            "pyannote_match_threshold": 0.7,
        },
    },
    "recording": {
        "mic_device": None,
        "system_capture": "auto",
        "sample_rate": 16000,
        "channels": 1,
        "last_project_id": None,
        "last_mic_device": None,
        "last_capture_system": None,
        # 録音停止忘れを防ぐための通知(自動停止はしない)。
        "stop_forget_reminder": {
            "enabled": True,
            "silence_sec": 300,
            "level_threshold": 0.02,
        },
        "mic_stream": {
            "latency": "high",
            "block_ms": 160,
            "max_block_ms": 320,
            "overflow_reopen_streak": 6,
            "max_read_errors": 50,
            "max_reopen": 8,
            "reopen_on_overflow": False,
            "reset_backend_on_internal_error": True,
            "internal_error_retries": 2,
            "internal_error_retry_delay_sec": 0.4,
        },
        # system track 遅延補正は transcript タイムラインとの整合を崩しやすいため、
        # 既定では無効。必要時のみ明示的に有効化する。
        "system_delay_compensation_enabled": False,
        # 内部音声が途中で途切れて mic のみの combined.flac が正常扱いされるのを防ぐ。
        "system_capture_watchdog": {
            "enabled": True,
            "min_duration_sec": 60,
            "min_coverage_ratio": 0.85,
            "max_missing_sec": 20,
        },
        # macOS 側の入力ゲイン変化やシステム音声側の音量低下を吸収する。
        "audio_leveling": {
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
        },
        "finalize_timeout_sec": 300,
        # 完了済みパイプラインの保持上限(メモリガード)。
        "memory_guard": {
            "max_retained_pipelines": 40,
            "max_transcript_segments_per_pipeline": 600,
        },
    },
    "agent": {
        "max_steps": 15,
        "max_file_lines": 500,
        "context_timeout_sec": 60,
        "max_context_tokens": 28000,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 18900,
        # CORS/WS の追加許可 Origin (必要時のみ設定)
        "allowed_origins": [],
        # 既定はローカル接続のみ許可。LAN公開が必要な場合のみ true にする。
        "allow_remote_clients": False,
    },
    "logging": {
        "dir": "~/.seam/logs/",
        "level": "INFO",
        "max_size_mb": 50,
        "backup_count": 3,
    },
    "setup": {
        "completed": False,
    },
    "debug": {
        "enabled": False,
        "watchdog_stall_sec": 60.0,
        "log_tail_lines": 120,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = deepcopy(v)
    return result


class Config:
    def __init__(self) -> None:
        self._path = APP_DIR / "config.yaml"
        self._data: dict[str, Any] = {}
        self._ensure_dir()
        self.load()

    def _ensure_dir(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            self._data = _deep_merge(DEFAULTS, raw)
            self._normalize()
        else:
            self._data = deepcopy(DEFAULTS)
            self._normalize()
            self.save()

    def save(self) -> None:
        self._ensure_dir()
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, default_flow_style=False, allow_unicode=True)

    def get(self, *keys: str, default: Any = None) -> Any:
        current = self._data
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key, default)
            else:
                return default
        return current

    def set(self, *keys: str, value: Any) -> None:
        if not keys:
            return
        current = self._data
        for key in keys[:-1]:
            current = current.setdefault(key, {})
        current[keys[-1]] = value

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def update(self, new_data: dict[str, Any]) -> None:
        self._data = _deep_merge(self._data, new_data)
        self._normalize()
        self.save()

    def _migrate_minutes_ai(self) -> None:
        """旧 (ollama.minutes_*) → 新 (minutes_ai.ollama.*) への一回限り移行。

        legacy keyがある = ユーザーが過去に明示的に設定した値、なので
        deep_merge 由来の default 値より優先する (上書き)。
        移行後に legacy key は呼び出し元 (_normalize 末尾) で削除される。
        """
        old = self._data.get("ollama")
        if not isinstance(old, dict):
            return
        new_ai = self._data.setdefault("minutes_ai", {})
        if not isinstance(new_ai, dict):
            new_ai = {}
            self._data["minutes_ai"] = new_ai
        new_ollama = new_ai.setdefault("ollama", {})
        if not isinstance(new_ollama, dict):
            new_ollama = {}
            new_ai["ollama"] = new_ollama

        mapping = {
            "minutes_model": "model",
            "minutes_num_ctx": "num_ctx",
            "minutes_num_thread": "num_thread",
            "minutes_num_batch": "num_batch",
            "minutes_num_gpu": "num_gpu",
            "minutes_low_vram": "low_vram",
            "minutes_dynamic_budget_enabled": "dynamic_budget_enabled",
            "minutes_spare_usage_ratio": "spare_usage_ratio",
            "minutes_keep_alive_sec": "keep_alive_sec",
        }
        for old_key, new_key in mapping.items():
            if old_key in old:
                # legacy keyがある場合は明示的なユーザー値なので default より優先
                new_ollama[new_key] = old[old_key]

    def _normalize(self) -> None:
        # ─── minutes_ai migration & 正規化 ─────────────────
        # 旧 (~ v1): ollama.minutes_* に要約モデル設定が混ざっていた
        # 新       : minutes_ai.ollama.* に整理 + 複数provider対応
        self._migrate_minutes_ai()

        ai = self._data.setdefault("minutes_ai", {})
        if not isinstance(ai, dict):
            ai = {}
            self._data["minutes_ai"] = ai

        # provider 正規化
        valid_providers = {
            "ollama", "claude_api", "openai", "gemini", "claude_code", "codex",
        }
        provider = str(ai.get("provider", "ollama")).strip()
        ai["provider"] = provider if provider in valid_providers else "ollama"
        ai["auto_generate"] = bool(ai.get("auto_generate", True))
        ai["generate_title"] = bool(ai.get("generate_title", True))
        # auto_dictionary_update は旧 auto_correct_dictionary を継承 (後方互換)
        if "auto_dictionary_update" not in ai:
            ai["auto_dictionary_update"] = bool(ai.get("auto_correct_dictionary", True))
        ai["auto_dictionary_update"] = bool(ai["auto_dictionary_update"])
        # 古いキーは migration 完了として除去
        ai.pop("auto_correct_dictionary", None)
        try:
            ai["timeout_sec"] = max(30, min(1800, int(ai.get("timeout_sec", 300))))
        except Exception:
            ai["timeout_sec"] = 300
        # 旧 keys 削除 (v1 設計レビューで除外した legacy provider)
        ai.pop("codex", None)
        ai.pop("claude", None)

        consent = ai.setdefault("consent", {})
        if not isinstance(consent, dict):
            consent = {}
            ai["consent"] = consent
        for cloud in ("claude_api", "openai", "gemini"):
            consent[cloud] = bool(consent.get(cloud, False))

        # provider別サブセクションの正規化
        ai_ollama = ai.setdefault("ollama", {})
        if not isinstance(ai_ollama, dict):
            ai_ollama = {}
            ai["ollama"] = ai_ollama
        ai_ollama.setdefault("base_url", "http://localhost:11434")
        ai_ollama.setdefault("model", "qwen3:8b")
        ai_ollama.setdefault("num_ctx", 8192)
        ai_ollama.setdefault("num_thread", 1)
        ai_ollama.setdefault("num_batch", 8)
        ai_ollama.setdefault("num_gpu", -1)  # all layers GPU (Mac Metal 最適化)
        ai_ollama.setdefault("low_vram", False)
        ai_ollama.setdefault("dynamic_budget_enabled", True)
        ai_ollama.setdefault("spare_usage_ratio", 0.6)
        ai_ollama.setdefault("keep_alive_sec", 0)
        try:
            ai_ollama["num_ctx"] = max(2048, min(131072, int(ai_ollama.get("num_ctx", 8192))))
        except Exception:
            ai_ollama["num_ctx"] = 8192
        try:
            ai_ollama["num_thread"] = max(1, min(16, int(ai_ollama.get("num_thread", 1))))
        except Exception:
            ai_ollama["num_thread"] = 1
        try:
            ai_ollama["num_batch"] = max(8, min(512, int(ai_ollama.get("num_batch", 8))))
        except Exception:
            ai_ollama["num_batch"] = 8
        try:
            # -1 = 全レイヤーGPU、0 = CPU、>=1 = 指定レイヤー数
            ai_ollama["num_gpu"] = max(-1, min(128, int(ai_ollama.get("num_gpu", -1))))
        except Exception:
            ai_ollama["num_gpu"] = -1
        ai_ollama["low_vram"] = bool(ai_ollama.get("low_vram", False))
        ai_ollama["dynamic_budget_enabled"] = bool(ai_ollama.get("dynamic_budget_enabled", True))
        try:
            ai_ollama["spare_usage_ratio"] = max(
                0.1, min(1.0, float(ai_ollama.get("spare_usage_ratio", 0.6)))
            )
        except Exception:
            ai_ollama["spare_usage_ratio"] = 0.6
        try:
            ai_ollama["keep_alive_sec"] = max(
                0, min(3600, int(ai_ollama.get("keep_alive_sec", 0)))
            )
        except Exception:
            ai_ollama["keep_alive_sec"] = 0

        # cloud provider セクション (model + max_tokens + 個別フラグ)
        ai_claude = ai.setdefault("claude_api", {})
        if not isinstance(ai_claude, dict):
            ai_claude = {}
            ai["claude_api"] = ai_claude
        ai_claude.setdefault("model", "claude-sonnet-4-6")
        ai_claude.setdefault("max_tokens", 8192)
        ai_claude.setdefault("use_prompt_caching", True)
        try:
            ai_claude["max_tokens"] = max(256, min(8192, int(ai_claude.get("max_tokens", 8192))))
        except Exception:
            ai_claude["max_tokens"] = 8192
        ai_claude["use_prompt_caching"] = bool(ai_claude.get("use_prompt_caching", True))

        ai_openai = ai.setdefault("openai", {})
        if not isinstance(ai_openai, dict):
            ai_openai = {}
            ai["openai"] = ai_openai
        ai_openai.setdefault("model", "gpt-4o-mini")
        ai_openai.setdefault("max_tokens", 8192)
        try:
            ai_openai["max_tokens"] = max(256, min(8192, int(ai_openai.get("max_tokens", 8192))))
        except Exception:
            ai_openai["max_tokens"] = 8192

        ai_gemini = ai.setdefault("gemini", {})
        if not isinstance(ai_gemini, dict):
            ai_gemini = {}
            ai["gemini"] = ai_gemini
        ai_gemini.setdefault("model", "gemini-2.0-flash")
        ai_gemini.setdefault("max_tokens", 8192)
        try:
            ai_gemini["max_tokens"] = max(256, min(8192, int(ai_gemini.get("max_tokens", 8192))))
        except Exception:
            ai_gemini["max_tokens"] = 8192

        # CLI providers (subprocess 経由、APIキー不要)
        ai_claude_code = ai.setdefault("claude_code", {})
        if not isinstance(ai_claude_code, dict):
            ai_claude_code = {}
            ai["claude_code"] = ai_claude_code
        ai_claude_code.setdefault("binary_path", "claude")
        ai_claude_code.setdefault("model", "sonnet")
        ai_claude_code.setdefault("launcher_command", "")
        ai_claude_code.setdefault("launcher_shell", "/bin/zsh")
        ai_claude_code.setdefault("launcher_interactive", True)
        if not isinstance(ai_claude_code.get("extra_args"), list):
            ai_claude_code["extra_args"] = []

        ai_codex = ai.setdefault("codex", {})
        if not isinstance(ai_codex, dict):
            ai_codex = {}
            ai["codex"] = ai_codex
        ai_codex.setdefault("binary_path", "codex")
        # 既定は空文字: --model を付けず CLI 側デフォルトを使う
        ai_codex.setdefault("model", "")
        ai_codex.setdefault("launcher_command", "")
        ai_codex.setdefault("launcher_shell", "/bin/zsh")
        ai_codex.setdefault("launcher_interactive", True)
        if not isinstance(ai_codex.get("extra_args"), list):
            ai_codex["extra_args"] = []

        # 旧 ollama サブツリーから minutes_* キーを除去 (migration 後の掃除)
        ollama = self._data.setdefault("ollama", {})
        if not isinstance(ollama, dict):
            ollama = {}
            self._data["ollama"] = ollama
        for legacy_key in (
            "minutes_model", "minutes_num_ctx", "minutes_num_thread",
            "minutes_num_batch", "minutes_num_gpu", "minutes_low_vram",
            "minutes_dynamic_budget_enabled", "minutes_spare_usage_ratio",
            "minutes_keep_alive_sec",
        ):
            ollama.pop(legacy_key, None)

        recording = self._data.setdefault("recording", {})
        if not isinstance(recording, dict):
            recording = {}
            self._data["recording"] = recording
        method = str(recording.get("system_capture", "auto") or "auto").strip().lower().replace("-", "_")
        method_aliases = {
            "coreaudio": "coreaudio_tap",
            "core_audio": "coreaudio_tap",
            "core_audio_tap": "coreaudio_tap",
            "process_tap": "coreaudio_tap",
            "tap": "coreaudio_tap",
            # 旧設定値。ScreenCaptureKit / BlackHole フォールバックは廃止したため auto に戻す。
            "sck": "auto",
            "screen_capture_kit": "auto",
            "screen_capturekit": "auto",
            "screencapturekit": "auto",
            "blackhole": "auto",
        }
        method = method_aliases.get(method, method)
        if method not in {"auto", "coreaudio_tap"}:
            method = "auto"
        recording["system_capture"] = method
        mic_stream = recording.setdefault("mic_stream", {})
        if not isinstance(mic_stream, dict):
            mic_stream = {}
            recording["mic_stream"] = mic_stream
        if "reopen_on_overflow" not in mic_stream:
            mic_stream["reopen_on_overflow"] = False
        mic_stream.setdefault("reset_backend_on_internal_error", True)
        mic_stream.setdefault("internal_error_retries", 2)
        mic_stream.setdefault("internal_error_retry_delay_sec", 0.4)

        leveling = recording.setdefault("audio_leveling", {})
        if not isinstance(leveling, dict):
            leveling = {}
            recording["audio_leveling"] = leveling
        leveling["enabled"] = bool(leveling.get("enabled", True))
        leveling["realtime_enabled"] = bool(leveling.get("realtime_enabled", True))
        leveling["final_normalize"] = bool(leveling.get("final_normalize", True))
        try:
            leveling["target_rms"] = max(0.01, min(0.30, float(leveling.get("target_rms", 0.08))))
        except Exception:
            leveling["target_rms"] = 0.08
        try:
            leveling["noise_floor"] = max(0.0001, min(0.05, float(leveling.get("noise_floor", 0.003))))
        except Exception:
            leveling["noise_floor"] = 0.003
        try:
            leveling["max_gain"] = max(1.0, min(20.0, float(leveling.get("max_gain", 12.0))))
        except Exception:
            leveling["max_gain"] = 12.0
        try:
            leveling["attack"] = max(0.01, min(1.0, float(leveling.get("attack", 0.18))))
        except Exception:
            leveling["attack"] = 0.18
        try:
            leveling["release"] = max(0.01, min(1.0, float(leveling.get("release", 0.55))))
        except Exception:
            leveling["release"] = 0.55
        try:
            leveling["peak_limit"] = max(0.50, min(1.0, float(leveling.get("peak_limit", 0.95))))
        except Exception:
            leveling["peak_limit"] = 0.95
        try:
            leveling["frame_ms"] = max(50, min(5000, int(leveling.get("frame_ms", 100))))
        except Exception:
            leveling["frame_ms"] = 100
        try:
            gauss_size = max(3, min(301, int(leveling.get("gauss_size", 3))))
            if gauss_size % 2 == 0:
                gauss_size += 1
            leveling["gauss_size"] = min(301, gauss_size)
        except Exception:
            leveling["gauss_size"] = 3
        try:
            recording["finalize_timeout_sec"] = max(
                60, min(900, int(recording.get("finalize_timeout_sec", 300)))
            )
        except Exception:
            recording["finalize_timeout_sec"] = 300
        watchdog = recording.setdefault("system_capture_watchdog", {})
        if not isinstance(watchdog, dict):
            watchdog = {}
            recording["system_capture_watchdog"] = watchdog
        watchdog["enabled"] = bool(watchdog.get("enabled", True))
        try:
            watchdog["min_duration_sec"] = max(
                10, min(600, float(watchdog.get("min_duration_sec", 60)))
            )
        except Exception:
            watchdog["min_duration_sec"] = 60.0
        try:
            watchdog["min_coverage_ratio"] = max(
                0.10, min(1.0, float(watchdog.get("min_coverage_ratio", 0.85)))
            )
        except Exception:
            watchdog["min_coverage_ratio"] = 0.85
        try:
            watchdog["max_missing_sec"] = max(
                5, min(600, float(watchdog.get("max_missing_sec", 20)))
            )
        except Exception:
            watchdog["max_missing_sec"] = 20.0

        whisper = self._data.setdefault("whisper", {})
        if not isinstance(whisper, dict):
            whisper = {}
            self._data["whisper"] = whisper
        speaker_memory = whisper.setdefault("speaker_memory", {})
        if not isinstance(speaker_memory, dict):
            speaker_memory = {}
            whisper["speaker_memory"] = speaker_memory
        speaker_memory["enabled"] = bool(speaker_memory.get("enabled", True))
        try:
            speaker_memory["match_threshold"] = max(
                0.0, min(0.99, float(speaker_memory.get("match_threshold", 0.82)))
            )
        except Exception:
            speaker_memory["match_threshold"] = 0.82
        try:
            speaker_memory["min_audio_sec"] = max(
                0.4, min(6.0, float(speaker_memory.get("min_audio_sec", 1.0)))
            )
        except Exception:
            speaker_memory["min_audio_sec"] = 1.0
        try:
            speaker_memory["sample_sec"] = max(
                0.8, min(8.0, float(speaker_memory.get("sample_sec", 2.5)))
            )
        except Exception:
            speaker_memory["sample_sec"] = 2.5
        try:
            speaker_memory["global_match_threshold"] = max(
                0.7, min(0.999, float(speaker_memory.get("global_match_threshold", 0.992)))
            )
        except Exception:
            speaker_memory["global_match_threshold"] = 0.992
        try:
            speaker_memory["rediarize_keep_ratio"] = max(
                0.6, min(0.99, float(speaker_memory.get("rediarize_keep_ratio", 0.86)))
            )
        except Exception:
            speaker_memory["rediarize_keep_ratio"] = 0.86
        provider = str(speaker_memory.get("diarization_provider", "legacy")).lower()
        speaker_memory["diarization_provider"] = (
            "pyannote" if provider == "pyannote" else "legacy"
        )
        speaker_memory.setdefault("pyannote_model", "pyannote/speaker-diarization-3.1")
        device = str(speaker_memory.get("pyannote_device", "auto")).lower()
        speaker_memory["pyannote_device"] = device if device in {"auto", "cpu", "mps"} else "auto"
        for key in ("pyannote_min_speakers", "pyannote_max_speakers"):
            v = speaker_memory.get(key)
            if v is None or v == "":
                speaker_memory[key] = None
            else:
                try:
                    iv = int(v)
                    speaker_memory[key] = max(1, min(20, iv))
                except Exception:
                    speaker_memory[key] = None
        try:
            speaker_memory["pyannote_match_threshold"] = max(
                0.3, min(0.95, float(speaker_memory.get("pyannote_match_threshold", 0.7)))
            )
        except Exception:
            speaker_memory["pyannote_match_threshold"] = 0.7

        server = self._data.setdefault("server", {})
        if not isinstance(server, dict):
            server = {}
            self._data["server"] = server
        allowed_origins = server.get("allowed_origins", [])
        if not isinstance(allowed_origins, list):
            allowed_origins = []
        cleaned_origins: list[str] = []
        for item in allowed_origins:
            value = str(item or "").strip()
            if value:
                cleaned_origins.append(value)
        server["allowed_origins"] = cleaned_origins
        server["allow_remote_clients"] = bool(server.get("allow_remote_clients", False))


config = Config()
