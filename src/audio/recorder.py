"""2トラック録音: マイク + ScreenCaptureKit (内部音声)

- mic: sounddevice → WAV 直接書き込み
- system: ScreenCaptureKit → WAV 直接書き込み
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


class Recorder:
    def __init__(self) -> None:
        self._recording = False
        self._mic_thread: threading.Thread | None = None
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
        self._mic_overflow_total: int = 0
        self._mic_reopen_total: int = 0
        self._last_overflow_log_at: float = 0.0
        # ScreenCaptureKit の初回PCM到着遅延(録音開始からの秒)。
        # システム音声ファイルには先頭無音が入らない場合があるため、最終ミックス時に補正する。
        self._system_first_pcm_delay_sec: float | None = None
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
    def elapsed_sec(self) -> float:
        if not self._recording:
            return 0
        return time.monotonic() - self._start_time

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

        self._mic_device = mic_device
        self._level_callback = level_callback
        self._pcm_callback = pcm_callback
        self._system_pcm_callback = system_pcm_callback
        self._error = None
        self._sys_capture = None
        self._mic_overflow_total = 0
        self._mic_reopen_total = 0
        self._last_overflow_log_at = 0.0
        self._system_first_pcm_delay_sec = None

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
                else:
                    sys_error = "ScreenCaptureKit が利用できません"
            except Exception as e:
                sys_error = str(e)
                logger.warning("System capture failed: %s", e)
                self._sys_capture = None

        logger.info("Recording started (mic=%s, system=%s, session=%s)",
                     mic_device, has_system, self._session_id)

        return {
            "session_id": self._session_id,
            "mic_device": mic_device,
            "has_system_audio": has_system,
            "system_error": sys_error,
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
            max_block_ms = max(block_ms, int(stream_cfg.get("max_block_ms", 300)))
            max_read_errors = max(10, int(stream_cfg.get("max_read_errors", 50)))
            max_stream_reopen = min(20, max(5, int(stream_cfg.get("max_reopen", 8))))
            overflow_reopen_streak = max(3, int(stream_cfg.get("overflow_reopen_streak", 6)))
            reopen_on_overflow = bool(stream_cfg.get("reopen_on_overflow", False))

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
            max_block_size = int(capture_rate * max_block_ms / 1000)

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
                            self._mic_overflow_total += 1
                            now = time.monotonic()
                            if (now - self._last_overflow_log_at) >= 1.0:
                                logger.warning(
                                    "mic: buffer overflowed (streak=%d, total=%d, block=%dms)",
                                    overflow_streak,
                                    self._mic_overflow_total,
                                    int(block_size / capture_rate * 1000),
                                )
                                self._last_overflow_log_at = now
                            if reopen_on_overflow and overflow_streak >= overflow_reopen_streak:
                                logger.warning(
                                    "mic: overflow streak reached (%d), reopening stream",
                                    overflow_streak,
                                )
                                if block_size < max_block_size:
                                    new_block = min(max_block_size, int(block_size * 1.5))
                                    if new_block <= block_size:
                                        new_block = min(max_block_size, block_size + int(capture_rate * 0.02))
                                    if new_block > block_size:
                                        logger.warning(
                                            "mic: increasing block size %dms -> %dms",
                                            int(block_size / capture_rate * 1000),
                                            int(new_block / capture_rate * 1000),
                                        )
                                        block_size = new_block
                                break
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
        self._recording = False

        # Wait for mic thread
        if self._mic_thread:
            self._mic_thread.join(timeout=5)
            self._mic_thread = None

        # Stop system capture
        if self._sys_capture:
            try:
                self._sys_capture.stop()
            except Exception as e:
                logger.warning("System capture stop error: %s", e)
            self._sys_capture = None

        # Check files
        mic_ok = self._mic_wav and self._mic_wav.exists() and self._mic_wav.stat().st_size > 44
        sys_ok = self._sys_wav and self._sys_wav.exists() and self._sys_wav.stat().st_size > 44

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
            "error": self._error,
            "system_first_pcm_delay_sec": round(self._system_first_pcm_delay_sec, 3)
            if self._system_first_pcm_delay_sec is not None else None,
        }

        logger.info("Recording stopped. Duration: %.1fs, File: %s", duration, wav_path)
        return result

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
        cmd: list[str] = [FFMPEG, "-y"]
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
        if mic and sys:
            if delay_ms > 0:
                cmd += [
                    "-i", str(mic), "-i", str(sys),
                    "-filter_complex",
                    f"[1:a]adelay={delay_ms}[sysd];[0:a][sysd]amix=inputs=2:duration=longest",
                ]
            else:
                cmd += [
                    "-i", str(mic), "-i", str(sys),
                    "-filter_complex", "amix=inputs=2:duration=longest",
                ]
        else:
            cmd += ["-i", str(mic or sys)]
            if sys and delay_ms > 0:
                cmd += ["-af", f"adelay={delay_ms}"]
        cmd += [
            "-ar", "24000", "-ac", "1",
            "-c:a", "flac", "-compression_level", "8",
            str(combined),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
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

    def get_status(self) -> dict:
        return {
            "recording": self._recording,
            "elapsed_sec": round(self.elapsed_sec, 1) if self._recording else 0,
            "mic_overflow_total": self._mic_overflow_total,
            "mic_reopen_total": self._mic_reopen_total,
            "mic_muted": self._mic_muted,
            "system_first_pcm_delay_sec": round(self._system_first_pcm_delay_sec, 3)
            if self._system_first_pcm_delay_sec is not None else None,
        }


recorder = Recorder()
