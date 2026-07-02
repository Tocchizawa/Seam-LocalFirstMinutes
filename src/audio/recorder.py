"""2トラック録音: マイク + Core Audio Tap (内部音声)

- mic: Core Audio sidecar → WAV 直接書き込み
- system: Core Audio Tap sidecar → WAV 直接書き込み
- 停止後に ffmpeg で combined.flac (24kHz mono) を生成し、中間 WAV は削除
"""
from __future__ import annotations

from collections.abc import Callable
import logging
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from src.audio.leveling import build_ffmpeg_loudness_filter
from src.config import APP_DIR, config

logger = logging.getLogger(__name__)

SESSIONS_DIR = APP_DIR / "sessions"
SAMPLE_RATE = 16000
AudioCallback = Callable[..., None]


def _find_ffmpeg() -> str:
    """ffmpeg のフルパスを返す。

    優先順:
    1. imageio_ffmpeg が pip インストールに同梱する静的 ffmpeg (配布ビルドはこれ)
    2. PATH 上の ffmpeg
    3. Homebrew の標準パス
    どれも見つからなければ ``ffmpeg`` 文字列を返す (実行時に PATH 検索される)
    """
    # 1. imageio_ffmpeg 経由 (配布ビルドではこれが当たる)
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass

    # 2. PATH
    path = shutil.which("ffmpeg")
    if path:
        return path
    # 3. Homebrew の標準パス
    for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if Path(p).exists():
            return p
    return "ffmpeg"


FFMPEG = _find_ffmpeg()


def _flac_duration_sec(path: Path) -> float:
    """FLAC ファイルの STREAMINFO メタブロックから duration を直接計算する。

    FLAC は先頭 4 バイト "fLaC" + 必須の STREAMINFO (4B header + 34B body)。
    body 内に sample rate (20bit) と total samples (36bit) が入っている。
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                return 0.0
            f.read(4)  # block header
            body = f.read(34)
        if len(body) < 18:
            return 0.0
        sr = (body[10] << 12) | (body[11] << 4) | (body[12] >> 4)
        total = ((body[13] & 0x0F) << 32) | (body[14] << 24) | (body[15] << 16) | (body[16] << 8) | body[17]
        if sr <= 0:
            return 0.0
        return total / sr
    except Exception:
        return 0.0


def _wav_duration_sec(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            if rate <= 0:
                return 0.0
            return wf.getnframes() / rate
    except Exception:
        return 0.0


def _audio_duration_sec(path: Path) -> float:
    if path.suffix.lower() == ".flac":
        return _flac_duration_sec(path)
    return _wav_duration_sec(path)


class Recorder:
    def __init__(self) -> None:
        self._recording = False
        self._mic_thread: threading.Thread | None = None
        self._mic_capture = None
        self._mic_backend: str | None = None
        self._sys_capture = None
        self._start_time: float = 0
        self._session_dir: Path | None = None
        self._session_id: str = ""
        self._mic_device: int | None = None
        self._mic_wav: Path | None = None
        self._sys_wav: Path | None = None
        self._level_callback: AudioCallback | None = None
        self._pcm_callback: AudioCallback | None = None
        self._system_pcm_callback: AudioCallback | None = None
        self._error: str | None = None
        self._mic_reopen_total: int = 0
        self._last_overflow_log_at: float = 0.0
        # システム音声の初回PCM到着遅延(録音開始からの秒)。
        # システム音声ファイルには先頭無音が入らない場合があるため、最終ミックス時に補正する。
        self._system_first_pcm_delay_sec: float | None = None
        self._capture_system_requested: bool = False
        self._system_capture_started: bool = False
        self._system_capture_start_error: str | None = None
        # マイクのみソフトミュート (system audio は変わらず). ミュート中も
        # wav には無音が書き込まれて時系列のオフセットを維持。
        self._mic_muted: bool = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def set_mic_muted(self, muted: bool) -> None:
        self._mic_muted = bool(muted)
        logger.info("Mic muted = %s", self._mic_muted)

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def mic_stream_alive(self) -> bool:
        if self._mic_capture is not None:
            return bool(getattr(self._mic_capture, "running", False))
        t = self._mic_thread
        return bool(t and t.is_alive())

    @property
    def elapsed_sec(self) -> float:
        if not self._recording:
            return 0
        return time.monotonic() - self._start_time

    def _default_input_device_index(self) -> int | None:
        try:
            dev = sd.default.device
        except Exception:
            return None
        candidate = None
        if isinstance(dev, (list, tuple)):
            candidate = dev[0] if len(dev) >= 1 else None
        else:
            candidate = dev
        if candidate is None:
            return None
        try:
            idx = int(candidate)
        except Exception:
            return None
        return idx if idx >= 0 else None

    def _is_portaudio_internal_error(self, error: object) -> bool:
        return "PaErrorCode -9986" in str(error)

    def _mic_internal_error_recovery_settings(self) -> tuple[bool, int, float]:
        stream_cfg = config.get("recording", "mic_stream", default={}) or {}
        reset_enabled = bool(stream_cfg.get("reset_backend_on_internal_error", True))
        try:
            retries = int(stream_cfg.get("internal_error_retries", 2))
        except Exception:
            retries = 2
        try:
            delay_sec = float(stream_cfg.get("internal_error_retry_delay_sec", 0.4))
        except Exception:
            delay_sec = 0.4
        return reset_enabled, max(0, min(retries, 5)), max(0.0, min(delay_sec, 3.0))

    def _reset_audio_backend(self, reason: str) -> bool:
        terminate = getattr(sd, "_terminate", None)
        initialize = getattr(sd, "_initialize", None)
        if not (callable(terminate) and callable(initialize)):
            logger.warning("PortAudio reset is unavailable after %s", reason)
            return False
        try:
            logger.warning("Resetting PortAudio after %s", reason)
            terminate()
            time.sleep(0.2)
            initialize()
            return True
        except Exception as e:
            logger.warning("PortAudio reset failed after %s: %s", reason, e)
            return False

    def _build_mic_start_error_message(
        self,
        raw_error: str,
        requested_device: int | None,
        default_device: int | None,
        fallback_attempted: bool,
    ) -> str:
        kind = "マイク入力を開始できませんでした"
        if "PaErrorCode -9986" in raw_error:
            kind = "PortAudio内部エラー(-9986)でマイク入力を開始できませんでした"
        elif "PaErrorCode -9996" in raw_error:
            kind = "選択したマイクデバイスが無効です (PaErrorCode -9996)"
        elif "PaErrorCode -9998" in raw_error:
            kind = "選択したマイクのチャンネル設定が不正です (PaErrorCode -9998)"
        elif "PaErrorCode -9997" in raw_error:
            kind = "選択したマイクのサンプルレート設定が不正です (PaErrorCode -9997)"

        requested = (
            f"選択デバイスID={requested_device}"
            if requested_device is not None
            else "選択デバイス=default"
        )
        default_hint = (
            f"既定入力デバイスID={default_device}"
            if default_device is not None
            else "既定入力デバイスなし"
        )
        fallback_hint = " / 既定入力デバイスへのフォールバックも失敗"
        if not fallback_attempted:
            fallback_hint = ""
        if self._is_portaudio_internal_error(raw_error):
            hint = (
                "CoreAudio/PortAudioの一時的な不調、長時間常駐後の音声入力復帰失敗、"
                "入力デバイスの切断/占有、またはmacOSのマイク権限が原因の可能性があります。"
                "アプリ再起動またはデバイス再選択で復旧する場合があります。"
            )
        else:
            hint = "macOSのマイク権限・入力デバイス接続・他アプリ占有・デバイス設定を確認して再試行してください。"
        return f"{kind}。{hint} ({requested} / {default_hint}{fallback_hint}) 元エラー: {raw_error}"

    def _probe_input_stream(self, device_idx: int) -> None:
        dev_info = sd.query_devices(device_idx)
        max_in_ch = int(dev_info.get("max_input_channels") or 0)
        if max_in_ch <= 0:
            raise RuntimeError(f"入力チャンネルがありません (device={device_idx})")

        native_rate = int(dev_info["default_samplerate"])
        recording_cfg = config.get("recording", default={}) or {}
        preferred_rate_raw = recording_cfg.get("sample_rate", SAMPLE_RATE)
        try:
            preferred_rate = int(preferred_rate_raw)
        except Exception:
            preferred_rate = SAMPLE_RATE
        preferred_rate = max(8000, min(48000, preferred_rate))

        old_device = self._mic_device
        self._mic_device = device_idx
        try:
            capture_rate = self._pick_input_sample_rate(preferred_rate, native_rate, max_in_ch)
            channels = self._pick_input_channels(capture_rate, max_in_ch)
        finally:
            self._mic_device = old_device

        stream_cfg = config.get("recording", "mic_stream", default={}) or {}
        latency = str(stream_cfg.get("latency", "high")).lower()
        if latency not in {"high", "low"}:
            latency = "high"
        block_ms = max(40, int(stream_cfg.get("block_ms", 100)))
        block_size = int(capture_rate * block_ms / 1000)

        stream = sd.InputStream(
            device=device_idx,
            samplerate=capture_rate,
            channels=channels,
            dtype="int16",
            blocksize=block_size,
            latency=latency,
        )
        try:
            stream.start()
        finally:
            try:
                stream.stop()
            except Exception:
                pass
            stream.close()

    def _prepare_mic_device_for_start(self, requested_device: int | None) -> int:
        default_device = self._default_input_device_index()
        candidates: list[int] = []
        if requested_device is not None:
            candidates.append(int(requested_device))
        elif default_device is not None:
            candidates.append(default_device)

        fallback_attempted = False
        if (
            requested_device is not None
            and default_device is not None
            and default_device != int(requested_device)
        ):
            candidates.append(default_device)
            fallback_attempted = True

        if not candidates:
            raise RuntimeError(
                self._build_mic_start_error_message(
                    "入力デバイスが見つかりません",
                    requested_device,
                    default_device,
                    fallback_attempted=False,
                )
            )

        last_error: Exception | None = None
        chosen: int | None = None
        reset_enabled, internal_retries, retry_delay_sec = self._mic_internal_error_recovery_settings()
        for idx, device_idx in enumerate(candidates):
            attempt = 0
            while True:
                attempt += 1
                try:
                    self._probe_input_stream(device_idx)
                    chosen = device_idx
                    if idx > 0:
                        logger.warning(
                            "Mic preflight fallback succeeded: requested=%s -> default=%s",
                            requested_device,
                            device_idx,
                        )
                    break
                except Exception as e:
                    last_error = e
                    is_internal = self._is_portaudio_internal_error(e)
                    can_retry = reset_enabled and is_internal and attempt <= internal_retries
                    if can_retry:
                        logger.warning(
                            "Mic preflight failed with PortAudio internal error "
                            "(device=%s, attempt=%d/%d): %s",
                            device_idx,
                            attempt,
                            internal_retries + 1,
                            e,
                        )
                        reset_ok = self._reset_audio_backend(
                            f"mic preflight failed on device {device_idx}",
                        )
                        if not reset_ok:
                            logger.warning(
                                "Mic preflight recovery skipped because PortAudio reset failed "
                                "(device=%s)",
                                device_idx,
                            )
                            break
                        if retry_delay_sec > 0:
                            time.sleep(retry_delay_sec)
                        continue
                    logger.warning("Mic preflight failed (device=%s): %s", device_idx, e)
                    break
            if chosen is not None:
                break

        if chosen is None:
            raw = str(last_error) if last_error else "unknown"
            raise RuntimeError(
                self._build_mic_start_error_message(
                    raw,
                    requested_device,
                    default_device,
                    fallback_attempted=fallback_attempted,
                )
            )
        return chosen

    def _recording_sample_rate(self) -> int:
        recording_cfg = config.get("recording", default={}) or {}
        preferred_rate_raw = recording_cfg.get("sample_rate", SAMPLE_RATE)
        try:
            preferred_rate = int(preferred_rate_raw)
        except Exception:
            preferred_rate = SAMPLE_RATE
        return max(8000, min(48000, preferred_rate))

    def _mic_capture_method(self) -> str:
        raw = config.get("recording", "mic_capture", default="coreaudio_sidecar")
        method = str(raw or "coreaudio_sidecar").strip().lower().replace("-", "_")
        aliases = {
            "auto": "coreaudio_sidecar",
            "coreaudio": "coreaudio_sidecar",
            "core_audio": "coreaudio_sidecar",
            "sidecar": "coreaudio_sidecar",
            "mic_sidecar": "coreaudio_sidecar",
            "coreaudio_mic": "coreaudio_sidecar",
            "portaudio": "sounddevice",
            "sound_device": "sounddevice",
        }
        method = aliases.get(method, method)
        if method not in {"coreaudio_sidecar", "sounddevice"}:
            return "coreaudio_sidecar"
        return method

    def _resolve_mic_device_index_for_sidecar(self, requested_device: int | None) -> int | None:
        if requested_device is not None:
            try:
                return int(requested_device)
            except Exception:
                return None
        return self._default_input_device_index()

    def _mic_device_name(self, device_idx: int | None) -> str | None:
        if device_idx is None:
            return None
        try:
            dev_info = sd.query_devices(device_idx)
            if int(dev_info.get("max_input_channels") or 0) <= 0:
                return None
            name = str(dev_info.get("name") or "").strip()
            return name or None
        except Exception as e:
            logger.warning("Failed to resolve mic device name for sidecar (device=%s): %s", device_idx, e)
            return None

    def _start_coreaudio_mic_capture(self) -> None:
        from src.audio.mic_capture import CoreAudioMicCapture, is_available as mic_sidecar_available

        if not mic_sidecar_available():
            raise RuntimeError("Core Audio mic sidecar が利用できません")
        if self._mic_wav is None:
            raise RuntimeError("mic output path is not initialized")

        capture_rate = self._recording_sample_rate()
        device_name = self._mic_device_name(self._mic_device)
        mic_capture = CoreAudioMicCapture()
        mic_capture.start(
            self._mic_wav,
            sample_rate=capture_rate,
            device_name=device_name,
            external_callback=self._pcm_callback,
            level_callback=self._level_callback,
            muted_getter=lambda: self._mic_muted,
        )
        self._mic_capture = mic_capture
        self._mic_backend = mic_capture.backend
        logger.info(
            "Mic input: backend=%s, capture=%dHz, device=%s",
            self._mic_backend,
            capture_rate,
            device_name or "default",
        )

    def start(
        self,
        mic_device: int | None = None,
        capture_system: bool = False,
        level_callback: AudioCallback | None = None,
        pcm_callback: AudioCallback | None = None,
        system_pcm_callback: AudioCallback | None = None,
        session_id: str | None = None,
    ) -> dict:
        if self._recording:
            raise RuntimeError("Already recording")

        mic_method = self._mic_capture_method()
        if mic_method == "coreaudio_sidecar":
            self._mic_device = self._resolve_mic_device_index_for_sidecar(mic_device)
        else:
            self._mic_device = self._prepare_mic_device_for_start(mic_device)
        self._level_callback = level_callback
        self._pcm_callback = pcm_callback
        self._system_pcm_callback = system_pcm_callback
        self._error = None
        self._mic_capture = None
        self._mic_backend = None
        self._sys_capture = None
        self._mic_reopen_total = 0
        self._last_overflow_log_at = 0.0
        self._system_first_pcm_delay_sec = None
        self._capture_system_requested = bool(capture_system)
        self._system_capture_started = False
        self._system_capture_start_error = None

        # Session
        # API 層で採番済み ID があればそれを使い、録音音声と transcript の保存先を揃える。
        self._session_id = session_id or time.strftime("%Y%m%d_%H%M%S")
        self._session_dir = SESSIONS_DIR / self._session_id
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._mic_wav = self._session_dir / "mic.wav"
        self._sys_wav = self._session_dir / "system.wav"

        self._recording = True
        self._start_time = time.monotonic()

        # Start mic
        if mic_method == "coreaudio_sidecar":
            try:
                self._start_coreaudio_mic_capture()
            except Exception as e:
                self._recording = False
                self._error = str(e)
                logger.error("Mic capture failed: %s", e)
                raise
        else:
            self._mic_backend = "sounddevice"
            self._mic_thread = threading.Thread(target=self._record_mic, daemon=True)
            self._mic_thread.start()

        # Start system audio
        has_system = False
        sys_error = None
        if capture_system:
            try:
                from src.audio.system_capture import SystemAudioCapture, is_available
                if is_available():
                    rec_cfg = config.get("recording", default={}) or {}
                    sys_rate_raw = rec_cfg.get("sample_rate", SAMPLE_RATE)
                    try:
                        sys_rate = int(sys_rate_raw)
                    except Exception:
                        sys_rate = SAMPLE_RATE
                    sys_rate = max(8000, min(48000, sys_rate))
                    self._sys_capture = SystemAudioCapture()
                    self._sys_capture.start(
                        self._sys_wav,
                        sample_rate=sys_rate,
                        external_callback=self._on_system_pcm,
                    )
                    has_system = True
                    self._system_capture_started = True
                else:
                    sys_error = "内部音声キャプチャが利用できません"
            except Exception as e:
                sys_error = str(e)
                logger.warning("System capture failed: %s", e)
                self._sys_capture = None
            self._system_capture_start_error = sys_error

        logger.info("Recording started (mic=%s, system=%s, session=%s)",
                     self._mic_device, has_system, self._session_id)

        return {
            "session_id": self._session_id,
            "mic_device": self._mic_device,
            "mic_backend": self._mic_backend,
            "has_system_audio": has_system,
            "system_error": sys_error,
            "system_backend": self._sys_capture.backend if self._sys_capture else None,
        }

    def _on_system_pcm(self, samples, sample_rate: int = 48000) -> None:
        if self._system_first_pcm_delay_sec is None:
            try:
                delay = max(0.0, time.monotonic() - self._start_time)
                self._system_first_pcm_delay_sec = delay
                logger.info("System PCM first chunk delay: %.3fs", delay)
            except Exception:
                self._system_first_pcm_delay_sec = 0.0
        cb = self._system_pcm_callback
        if cb is None:
            return
        cb(samples, sample_rate)

    def _pick_input_channels(self, sample_rate: int, max_ch: int) -> int:
        """check_input_settings で 1ch 可否を確認。NG なら max_ch を返す。"""
        try:
            sd.check_input_settings(
                device=self._mic_device,
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
            )
            return 1
        except Exception:
            return max(1, min(max_ch, 2))

    def _pick_input_sample_rate(self, preferred_rate: int, native_rate: int, max_ch: int) -> int:
        """希望サンプルレートを試し、非対応ならデバイス既定へフォールバックする。"""
        preferred = int(preferred_rate) if preferred_rate else SAMPLE_RATE
        if preferred <= 0:
            preferred = SAMPLE_RATE
        if preferred == native_rate:
            return native_rate
        check_channels = [1]
        alt_ch = max(1, min(max_ch, 2))
        if alt_ch not in check_channels:
            check_channels.append(alt_ch)
        last_error: Exception | None = None
        for ch in check_channels:
            try:
                sd.check_input_settings(
                    device=self._mic_device,
                    samplerate=preferred,
                    channels=ch,
                    dtype="int16",
                )
                return preferred
            except Exception as e:
                last_error = e
        logger.info(
            "Mic sample_rate=%dHz is not supported, fallback to native %dHz (%s)",
            preferred,
            native_rate,
            last_error,
        )
        return native_rate

    def _record_mic(self) -> None:
        """マイク録音 → 設定レート優先で WAV 書き込み(非対応ならネイティブへフォールバック)。

        プチフリ等で stream.read() が一時的に失敗しても、ループを継続させる。
        - 1度の例外ではスレッド終了せず、短時間 sleep してリトライ
        - 連続失敗が threshold を超えたらストリームを再作成
        - 再作成も失敗が続いたら諦めてログ吐いて終了
        """
        wf = None
        try:
            if self._mic_device is not None:
                dev_info = sd.query_devices(self._mic_device)
            else:
                dev_info = sd.query_devices(sd.default.device[0])
            native_rate = int(dev_info["default_samplerate"])
            max_in_ch = int(dev_info.get("max_input_channels") or 1)
            recording_cfg = config.get("recording", default={}) or {}
            preferred_rate_raw = recording_cfg.get("sample_rate", SAMPLE_RATE)
            try:
                preferred_rate = int(preferred_rate_raw)
            except Exception:
                preferred_rate = SAMPLE_RATE
            preferred_rate = max(8000, min(48000, preferred_rate))
            capture_rate = self._pick_input_sample_rate(preferred_rate, native_rate, max_in_ch)
            stream_cfg = config.get("recording", "mic_stream", default={}) or {}
            latency = str(stream_cfg.get("latency", "high")).lower()
            if latency not in {"high", "low"}:
                latency = "high"
            block_ms = max(40, int(stream_cfg.get("block_ms", 100)))
            max_read_errors = max(10, int(stream_cfg.get("max_read_errors", 50)))
            max_stream_reopen = min(20, max(5, int(stream_cfg.get("max_reopen", 8))))
            reset_on_internal, internal_reset_retries, internal_retry_delay = (
                self._mic_internal_error_recovery_settings()
            )
            internal_resets = 0

            # 対応チャンネル数を確定: 1ch を試して NG なら 2ch (max_in_ch)
            channels = self._pick_input_channels(capture_rate, max_in_ch)
            logger.info(
                "Mic input: native=%dHz, capture=%dHz, %dch (max %d)",
                native_rate,
                capture_rate,
                channels,
                max_in_ch,
            )

            wf = wave.open(str(self._mic_wav), "wb")
            wf.setnchannels(1)  # 出力 WAV は常に mono(必要なら mix-down)
            wf.setsampwidth(2)
            wf.setframerate(capture_rate)

            block_size = int(capture_rate * block_ms / 1000)

            def _emit_mic_samples(mono: np.ndarray) -> None:
                try:
                    wf.writeframes(mono.tobytes())
                except Exception as e:
                    logger.error("WAV write error: %s", e)

                if self._pcm_callback:
                    try:
                        self._pcm_callback(mono, capture_rate)
                    except Exception as e:
                        logger.error("PCM callback error: %s", e)

                if self._level_callback:
                    try:
                        if self._mic_muted:
                            level = 0.0
                        else:
                            rms = np.sqrt(np.mean(mono.astype(np.float32) ** 2)) / 32768.0
                            level = min(1.0, rms * 5)
                        self._level_callback(level)
                    except Exception:
                        pass

            def _open_input_stream(ch: int) -> sd.InputStream:
                return sd.InputStream(
                    device=self._mic_device,
                    samplerate=capture_rate,
                    channels=ch,
                    dtype="int16",
                    blocksize=block_size,
                    latency=latency,
                )

            stream_opens = 0
            while self._recording and stream_opens <= max_stream_reopen:
                stream_opens += 1
                self._mic_reopen_total = max(0, stream_opens - 1)
                try:
                    stream = _open_input_stream(channels)
                    stream.start()
                except Exception as e:
                    msg = str(e)
                    # チャンネル数エラーの場合は別の値で再試行
                    if ("PaErrorCode -9998" in msg or "channel" in msg.lower()) and channels == 1 and max_in_ch >= 2:
                        logger.warning("1ch open failed, retrying with %dch", max_in_ch)
                        channels = max_in_ch
                        try:
                            stream = _open_input_stream(channels)
                            stream.start()
                        except Exception as e2:
                            logger.error("Stream open retry failed: %s", e2)
                            if stream_opens >= max_stream_reopen:
                                self._error = str(e2)
                                return
                            time.sleep(0.5)
                            continue
                    else:
                        logger.error("InputStream open failed (try %d/%d): %s",
                                     stream_opens, max_stream_reopen, e)
                        if (
                            reset_on_internal
                            and self._is_portaudio_internal_error(e)
                            and internal_resets < internal_reset_retries
                        ):
                            internal_resets += 1
                            reset_ok = self._reset_audio_backend(
                                f"mic stream open failed on device {self._mic_device}",
                            )
                            if reset_ok:
                                if internal_retry_delay > 0:
                                    time.sleep(internal_retry_delay)
                                continue
                        if stream_opens >= max_stream_reopen:
                            self._error = msg
                            return
                        time.sleep(0.5)
                        continue

                if stream_opens > 1:
                    logger.info(
                        "InputStream reopened (try %d/%d, block=%dms, latency=%s)",
                        stream_opens,
                        max_stream_reopen,
                        int(block_size / capture_rate * 1000),
                        latency,
                    )

                read_errors = 0
                overflow_streak = 0
                try:
                    while self._recording:
                        try:
                            data, overflowed = stream.read(block_size)
                            read_errors = 0
                        except Exception as e:
                            read_errors += 1
                            logger.warning("mic read failed (%d/%d): %s",
                                           read_errors, max_read_errors, e)
                            if read_errors >= max_read_errors:
                                logger.error("mic: too many consecutive errors, reopening stream")
                                break
                            time.sleep(0.05)
                            continue

                        if overflowed:
                            overflow_streak += 1
                            now = time.monotonic()
                            if (now - self._last_overflow_log_at) >= 1.0:
                                logger.warning(
                                    "mic: buffer overflowed (streak=%d, block=%dms)",
                                    overflow_streak,
                                    int(block_size / capture_rate * 1000),
                                )
                                self._last_overflow_log_at = now
                        else:
                            overflow_streak = 0

                        # ステレオ等を mono にミックスダウン
                        if channels > 1:
                            try:
                                mono = data.astype(np.int32).mean(axis=1).astype(np.int16)
                            except Exception:
                                mono = data[:, 0]  # 失敗時は左ch のみ
                        else:
                            mono = data.flatten() if data.ndim > 1 else data

                        # ミュート中はマイク経路を完全に無音化:
                        # - WAV には無音を書き込み続け、再生時の時間ずれを防ぐ
                        # - 文字起こし / レベル表示にも 0 を渡し、誤検出を防ぐ
                        if self._mic_muted:
                            mono = np.zeros_like(mono)

                        _emit_mic_samples(mono)
                finally:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass

            if stream_opens > max_stream_reopen:
                logger.error("mic: gave up after %d reopens", max_stream_reopen)
                self._error = "マイクストリームの再接続に失敗しました"

        except Exception as e:
            self._error = str(e)
            logger.error("Mic recording error: %s", e)
        finally:
            if wf:
                try:
                    wf.close()
                except Exception:
                    pass

    def stop(self) -> dict:
        if not self._recording:
            raise RuntimeError("Not recording")

        elapsed = self.elapsed_sec
        system_backend = self._sys_capture.backend if self._sys_capture else None
        self._recording = False

        if self._mic_capture:
            try:
                self._mic_capture.stop()
                if self._mic_capture.error:
                    mic_error = self._mic_capture.error
                    self._error = f"{self._error}; {mic_error}" if self._error else mic_error
            except Exception as e:
                logger.warning("Mic capture stop error: %s", e)
                self._error = f"{self._error}; {e}" if self._error else str(e)
            self._mic_capture = None

        # Wait for mic thread
        if self._mic_thread:
            self._mic_thread.join(timeout=5)
            self._mic_thread = None

        # Stop system capture
        if self._sys_capture:
            try:
                self._sys_capture.stop()
                if self._sys_capture.error:
                    sys_error = self._sys_capture.error
                    self._error = f"{self._error}; {sys_error}" if self._error else sys_error
            except Exception as e:
                logger.warning("System capture stop error: %s", e)
                self._error = f"{self._error}; {e}" if self._error else str(e)
            self._sys_capture = None

        # Check files
        mic_ok = self._mic_wav and self._mic_wav.exists() and self._mic_wav.stat().st_size > 44
        sys_ok = self._sys_wav and self._sys_wav.exists() and self._sys_wav.stat().st_size > 44
        mic_duration = _audio_duration_sec(self._mic_wav) if mic_ok and self._mic_wav else 0.0
        system_duration = _audio_duration_sec(self._sys_wav) if sys_ok and self._sys_wav else 0.0
        expected_system_duration = mic_duration or elapsed
        system_coverage_ratio = (
            system_duration / expected_system_duration
            if expected_system_duration > 0 and system_duration > 0 else None
        )
        system_coverage_error = self._validate_system_audio_coverage(
            mic_duration=mic_duration,
            system_duration=system_duration,
            elapsed=elapsed,
            sys_ok=bool(sys_ok),
        )
        if system_coverage_error:
            self._error = (
                f"{self._error}; {system_coverage_error}"
                if self._error else system_coverage_error
            )

        # FLAC 24kHz mono に統合(中間 mic/system WAV は削除)
        combined_wav = None
        if mic_ok or sys_ok:
            combined_wav = self._finalize_audio(
                self._mic_wav if mic_ok else None,
                self._sys_wav if sys_ok else None,
                system_delay_sec=self._system_first_pcm_delay_sec,
            )

        # FLAC 化に成功したら中間ファイルは削除済み。失敗時はフォールバックとして残す
        finalized = combined_wav is not None
        if finalized:
            mic_ok = self._mic_wav.exists() if self._mic_wav else False
            sys_ok = self._sys_wav.exists() if self._sys_wav else False

        wav_path = combined_wav or (self._mic_wav if mic_ok else (self._sys_wav if sys_ok else None))

        # Duration: WAV は wave で読める。FLAC は STREAMINFO を直接読む。
        duration = elapsed
        if wav_path and wav_path.exists():
            try:
                if wav_path.suffix.lower() == ".flac":
                    duration = _flac_duration_sec(wav_path) or elapsed
                else:
                    with wave.open(str(wav_path), "rb") as wf:
                        duration = wf.getnframes() / wf.getframerate()
            except Exception:
                duration = elapsed

        result = {
            "session_id": self._session_id,
            "session_dir": str(self._session_dir),
            "wav_path": str(wav_path) if wav_path else None,
            "mic_wav": str(self._mic_wav) if mic_ok else None,
            "system_wav": str(self._sys_wav) if sys_ok else None,
            "combined_wav": str(combined_wav) if combined_wav else None,
            "duration_sec": round(duration, 1),
            "elapsed_sec": round(elapsed, 1),
            "mic_duration_sec": round(mic_duration, 1) if mic_duration > 0 else None,
            "system_duration_sec": round(system_duration, 1) if system_duration > 0 else None,
            "system_coverage_ratio": round(system_coverage_ratio, 3)
            if system_coverage_ratio is not None else None,
            "system_backend": system_backend,
            "mic_backend": self._mic_backend,
            "error": self._error,
            "system_first_pcm_delay_sec": round(self._system_first_pcm_delay_sec, 3)
            if self._system_first_pcm_delay_sec is not None else None,
        }

        logger.info("Recording stopped. Duration: %.1fs, File: %s", duration, wav_path)
        return result

    def _system_coverage_settings(self) -> tuple[bool, float, float, float]:
        cfg = config.get("recording", "system_capture_watchdog", default={}) or {}
        enabled = bool(cfg.get("enabled", True))
        try:
            min_duration_sec = float(cfg.get("min_duration_sec", 60))
        except Exception:
            min_duration_sec = 60.0
        try:
            min_coverage_ratio = float(cfg.get("min_coverage_ratio", 0.85))
        except Exception:
            min_coverage_ratio = 0.85
        try:
            max_missing_sec = float(cfg.get("max_missing_sec", 20))
        except Exception:
            max_missing_sec = 20.0
        min_duration_sec = max(10.0, min(600.0, min_duration_sec))
        min_coverage_ratio = max(0.10, min(1.0, min_coverage_ratio))
        max_missing_sec = max(5.0, min(600.0, max_missing_sec))
        return enabled, min_duration_sec, min_coverage_ratio, max_missing_sec

    def _validate_system_audio_coverage(
        self,
        *,
        mic_duration: float,
        system_duration: float,
        elapsed: float,
        sys_ok: bool,
    ) -> str | None:
        if not self._capture_system_requested:
            return None

        if not self._system_capture_started:
            detail = self._system_capture_start_error or "unknown"
            return f"System audio capture did not start (内部音声の録音開始に失敗): {detail}"

        enabled, min_duration_sec, min_coverage_ratio, max_missing_sec = (
            self._system_coverage_settings()
        )
        if not enabled:
            return None
        if not sys_ok:
            return "System audio capture produced no usable audio (内部音声ファイルが空です)"

        expected = max(float(mic_duration or 0.0), float(elapsed or 0.0))
        if expected < min_duration_sec:
            return None

        missing = max(0.0, expected - float(system_duration or 0.0))
        coverage = float(system_duration or 0.0) / expected if expected > 0 else 0.0
        if missing > max_missing_sec and coverage < min_coverage_ratio:
            return (
                "System audio ended early (内部音声が途中で途切れた可能性があります): "
                f"system={system_duration:.1f}s expected={expected:.1f}s "
                f"coverage={coverage:.1%}"
            )
        return None

    def _finalize_audio(
        self,
        mic: Path | None,
        sys: Path | None,
        system_delay_sec: float | None = None,
    ) -> Path | None:
        """mic / system トラックを FLAC 24kHz mono の combined.flac に統合する。

        - 両方ある場合は amix で合成
        - 片方だけなら単一入力をそのままダウンサンプル
        - 成功したら中間 WAV (mic.wav / system.wav) は削除する
        """
        if mic is None and sys is None:
            return None
        combined = self._session_dir / "combined.flac"
        rec_cfg = config.get("recording", default={}) or {}
        loudness_filter = build_ffmpeg_loudness_filter(rec_cfg.get("audio_leveling"))
        delay_enabled = True
        delay_ms = 0
        try:
            timeout_sec = max(60, min(900, int(rec_cfg.get("finalize_timeout_sec", 300))))
        except Exception:
            timeout_sec = 300
        if delay_enabled:
            delay_ms = int(round(max(0.0, float(system_delay_sec or 0.0)) * 1000.0))
            # 200ms未満は実測誤差として扱い、補正を入れない。
            if delay_ms < 200:
                delay_ms = 0
            # 暴走値のガード: 10秒以上は異常値として無効化。
            if delay_ms > 10_000:
                logger.warning("Ignoring suspicious system delay: %dms", delay_ms)
                delay_ms = 0
            elif delay_ms > 0:
                logger.info("Applying system audio delay compensation: %dms", delay_ms)
        elif system_delay_sec:
            logger.info(
                "System delay measured (%.3fs) but compensation is disabled",
                float(system_delay_sec),
            )
        cmd = self._build_finalize_cmd(mic, sys, delay_ms, loudness_filter)
        fallback_cmd = (
            self._build_finalize_cmd(mic, sys, delay_ms, None)
            if loudness_filter else None
        )
        if loudness_filter:
            logger.info("Applying dynamic audio normalization to final mix")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if (
                result.returncode != 0
                and fallback_cmd is not None
                and "No such filter" in (result.stderr or "")
            ):
                logger.warning(
                    "ffmpeg dynamic normalization unsupported, retrying without it: %s",
                    result.stderr[-300:],
                )
                result = subprocess.run(
                    fallback_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                )
            if result.returncode != 0:
                logger.error("ffmpeg finalize failed (code %d): %s",
                             result.returncode, result.stderr[-500:])
                return None
            if not (combined.exists() and combined.stat().st_size > 44):
                logger.error("combined.flac not created or empty")
                return None

            # 中間 WAV を削除してディスク節約
            for src in (mic, sys):
                if src and src.exists():
                    try:
                        src.unlink()
                    except Exception as e:
                        logger.warning("Failed to remove intermediate %s: %s", src.name, e)

            logger.info("Finalized → %s (%.1f MB)",
                        combined.name, combined.stat().st_size / (1024 * 1024))
            return combined
        except Exception as e:
            logger.error("Finalize failed: %s", e)
        return None

    def _build_finalize_cmd(
        self,
        mic: Path | None,
        sys: Path | None,
        delay_ms: int = 0,
        loudness_filter: str | None = None,
    ) -> list[str]:
        combined = self._session_dir / "combined.flac"
        cmd: list[str] = [FFMPEG, "-y"]
        if mic and sys:
            mix_filter = "amix=inputs=2:duration=longest:normalize=0"
            if loudness_filter:
                mix_filter = f"{mix_filter},{loudness_filter}"
            if delay_ms > 0:
                cmd += [
                    "-i", str(mic), "-i", str(sys),
                    "-filter_complex",
                    f"[1:a]adelay={delay_ms}[sysd];[0:a][sysd]{mix_filter}",
                ]
            else:
                cmd += [
                    "-i", str(mic), "-i", str(sys),
                    "-filter_complex", mix_filter,
                ]
        else:
            cmd += ["-i", str(mic or sys)]
            filters: list[str] = []
            if sys and delay_ms > 0:
                filters.append(f"adelay={delay_ms}")
            if loudness_filter:
                filters.append(loudness_filter)
            if filters:
                cmd += ["-af", ",".join(filters)]
        cmd += [
            "-ar", "24000", "-ac", "1",
            "-c:a", "flac", "-compression_level", "8",
            str(combined),
        ]
        return cmd

    def get_status(self) -> dict:
        return {
            "recording": self._recording,
            "elapsed_sec": round(self.elapsed_sec, 1) if self._recording else 0,
            "mic_reopen_total": self._mic_reopen_total,
            "mic_stream_alive": self.mic_stream_alive,
            "mic_backend": self._mic_backend,
            "mic_muted": self._mic_muted,
            "error": self._error,
            "system_first_pcm_delay_sec": round(self._system_first_pcm_delay_sec, 3)
            if self._system_first_pcm_delay_sec is not None else None,
        }


recorder = Recorder()
