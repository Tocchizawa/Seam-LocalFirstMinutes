"""ScreenCaptureKit でシステム内部音声をキャプチャ (pyobjc)

macOS 13+ 必須。画面収録の権限が必要。
ドライバ不要、Python 完結。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import objc

logger = logging.getLogger(__name__)


def _find_ffmpeg() -> str:
    import shutil

    # 配布ビルドは imageio_ffmpeg 同梱の静的 binary を優先
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass

    path = shutil.which("ffmpeg")
    if path:
        return path
    for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if Path(p).exists():
            return p
    return "ffmpeg"


def is_available() -> bool:
    try:
        import ScreenCaptureKit  # noqa: F401
        return True
    except ImportError:
        return False


class SystemAudioCapture:
    def __init__(self) -> None:
        self._stream = None
        self._handler = None
        self._running = False
        self._wav_file = None
        self._error: str | None = None
        self._sample_rate: int = 48000

    @property
    def error(self) -> str | None:
        return self._error

    def start(self, output_path: Path, sample_rate: int = 48000,
              external_callback=None) -> None:
        if self._running:
            raise RuntimeError("Already capturing")

        import ScreenCaptureKit as SCK

        content = _get_shareable_content_sync()
        if content is None:
            raise RuntimeError("画面収録の権限がありません。システム設定 > プライバシーとセキュリティ > 画面収録 で許可してください")

        displays = content.displays()
        if not displays:
            raise RuntimeError("ディスプレイが見つかりません")

        target_rate = int(sample_rate) if sample_rate else 48000
        # ScreenCaptureKit の sampleRate は離散値のみ。
        if target_rate not in {8000, 16000, 24000, 48000}:
            target_rate = 48000
        self._sample_rate = target_rate

        config = SCK.SCStreamConfiguration.alloc().init()
        config.setCapturesAudio_(True)
        config.setExcludesCurrentProcessAudio_(False)
        config.setWidth_(2)
        config.setHeight_(2)
        config.setSampleRate_(self._sample_rate)
        config.setChannelCount_(1)

        filt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            displays[0], []
        )

        # 生の float32 バイナリをそのまま書き出す (変換は後で ffmpeg がやる)
        self._raw_path = output_path.with_suffix(".raw")
        self._wav_path = output_path
        self._raw_file = open(self._raw_path, "wb")

        self._handler = _create_handler(self._raw_file, external_callback, self._sample_rate)

        self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            filt, config, None
        )

        success, err = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._handler, 1, None, None
        )
        if not success:
            self._cleanup()
            raise RuntimeError(f"ストリーム出力の追加に失敗: {err}")

        # Start
        start_event = threading.Event()
        start_error = [None]

        def on_start(error):
            if error:
                start_error[0] = str(error)
            start_event.set()

        self._stream.startCaptureWithCompletionHandler_(on_start)
        if not start_event.wait(timeout=10):
            self._cleanup()
            self._error = "ScreenCaptureKit のキャプチャ開始がタイムアウトしました"
            raise RuntimeError(self._error)

        if start_error[0]:
            self._cleanup()
            self._error = f"キャプチャ開始に失敗: {start_error[0]}"
            raise RuntimeError(self._error)

        self._running = True
        logger.info(
            "System audio capture started (ScreenCaptureKit %dHz → %s)",
            self._sample_rate,
            output_path,
        )

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._stream:
            stop_event = threading.Event()
            self._stream.stopCaptureWithCompletionHandler_(lambda err: stop_event.set())
            stop_event.wait(timeout=5)

        # Close raw file
        if self._raw_file:
            self._raw_file.close()
            self._raw_file = None

        # ffmpeg: raw float32 mono (config sample_rate) → WAV mono
        if hasattr(self, "_raw_path") and self._raw_path and self._raw_path.exists():
            import subprocess
            try:
                result = subprocess.run([
                    _find_ffmpeg(), "-y",
                    "-f", "f32le", "-ar", str(self._sample_rate), "-ac", "1",
                    "-i", str(self._raw_path),
                    str(self._wav_path),
                ], capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    stderr = (result.stderr or "").strip()
                    raise RuntimeError(f"ffmpeg exited {result.returncode}: {stderr[-500:]}")
                if not (self._wav_path.exists() and self._wav_path.stat().st_size > 44):
                    raise RuntimeError("converted WAV was not created or is empty")
                self._raw_path.unlink(missing_ok=True)
                logger.info("System audio: raw → %s", self._wav_path.name)
            except Exception as e:
                self._error = f"System audio conversion failed: {e}"
                logger.error("System audio conversion failed: %s", e)

        self._stream = None
        self._handler = None
        logger.info("System audio capture stopped")

    def _cleanup(self) -> None:
        if self._raw_file:
            try:
                self._raw_file.close()
            except Exception:
                pass
            self._raw_file = None
        self._stream = None
        self._handler = None


def _get_shareable_content_sync():
    """SCShareableContent を同期取得"""
    import ScreenCaptureKit as SCK

    result = [None]
    event = threading.Event()

    def handler(content, error):
        if error:
            logger.error("SCShareableContent error: %s", error)
        else:
            result[0] = content
        event.set()

    SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        False, False, handler
    )
    event.wait(timeout=10)
    return result[0]


_AudioHandlerClass = None


def _get_handler_class():
    """ObjC クラスはプロセス内で一度しか登録できないのでキャッシュする。"""
    global _AudioHandlerClass
    if _AudioHandlerClass is not None:
        return _AudioHandlerClass

    import CoreMedia as _CM

    SCStreamOutput = objc.protocolNamed("SCStreamOutput")

    class AudioHandler(objc.lookUpClass("NSObject"), protocols=[SCStreamOutput]):
        _raw_file = objc.ivar("_raw_file")
        _external_callback = objc.ivar("_external_callback")
        _sample_rate = objc.ivar("_sample_rate")

        def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
            if output_type != 1:
                return
            f = self._raw_file
            if f is None:
                return
            try:
                block_buffer = _CM.CMSampleBufferGetDataBuffer(sample_buffer)
                if block_buffer is None:
                    return
                length = _CM.CMBlockBufferGetDataLength(block_buffer)
                if length == 0:
                    return
                data = bytearray(length)
                _CM.CMBlockBufferCopyDataBytes(block_buffer, 0, length, data)
                f.write(data)
                f.flush()
                cb = self._external_callback
                if cb is not None:
                    try:
                        import numpy as np
                        samples = np.frombuffer(bytes(data), dtype=np.float32)
                        cb(samples, int(self._sample_rate) if self._sample_rate else 48000)
                    except Exception as e:
                        logger.error("System PCM callback error: %s", e)
            except Exception as e:
                logger.error("Audio buffer error: %s", e)

    _AudioHandlerClass = AudioHandler
    return _AudioHandlerClass


def _create_handler(raw_file, external_callback=None, sample_rate: int = 48000):
    """SCStreamOutput ハンドラを生成し、書き込み先ファイルをセットして返す。"""
    cls = _get_handler_class()
    handler = cls.alloc().init()
    handler._raw_file = raw_file
    handler._external_callback = external_callback
    handler._sample_rate = int(sample_rate)
    return handler
