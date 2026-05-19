from __future__ import annotations
# ruff: noqa: E402

import asyncio
import logging
import os
import signal
import shutil
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from src.config import config
from src.logging_config import setup_logging
from src.security import (
    DEFAULT_LOCAL_ORIGINS,
    LOCAL_ORIGIN_REGEX,
    is_allowed_request_origin,
    is_loopback_client_host,
    normalize_origin_list,
    should_enforce_origin_for_request,
    should_enforce_loopback_for_request_path,
)
from src.startup_progress import emit as emit_progress

logger = logging.getLogger(__name__)
_startup_background_tasks: set[asyncio.Task] = set()


def _is_process_alive(pid: int) -> bool:
    """PID の生存確認。存在しない場合のみ False。"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _start_parent_watchdog() -> None:
    """親アプリ PID が消えたらバックエンドを終了する。"""
    raw_pid = (os.environ.get("SEAM_PARENT_PID") or "").strip()
    if not raw_pid:
        return
    try:
        parent_pid = int(raw_pid)
    except ValueError:
        logger.warning("invalid SEAM_PARENT_PID: %s", raw_pid)
        return
    if parent_pid <= 1:
        logger.warning("skip parent watchdog: invalid parent pid=%s", parent_pid)
        return

    interval = 1.5
    raw_interval = (os.environ.get("SEAM_PARENT_WATCHDOG_INTERVAL_SEC") or "").strip()
    if raw_interval:
        try:
            interval = max(0.5, float(raw_interval))
        except ValueError:
            logger.warning(
                "invalid SEAM_PARENT_WATCHDOG_INTERVAL_SEC: %s", raw_interval,
            )

    def _watch() -> None:
        logger.info(
            "parent watchdog started: self_pid=%s parent_pid=%s interval=%.1fs",
            os.getpid(),
            parent_pid,
            interval,
        )
        while True:
            if not _is_process_alive(parent_pid):
                logger.warning(
                    "parent pid disappeared (parent_pid=%s). shutting down backend",
                    parent_pid,
                )
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                except Exception:
                    os._exit(0)
                return
            time.sleep(interval)

    t = threading.Thread(target=_watch, name="parent-watchdog", daemon=True)
    t.start()


def _ensure_ffmpeg_on_path() -> None:
    """ffmpeg を subprocess で起動できるよう PATH に追加。
    .app から spawn された場合 shell の PATH を継承しないので、
    homebrew 等の典型パスを明示的に prepend する。"""
    if shutil.which("ffmpeg"):
        return
    candidates = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        os.path.expanduser("~/.local/bin"),
    ]
    for d in candidates:
        if os.path.exists(os.path.join(d, "ffmpeg")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return


_ensure_ffmpeg_on_path()
emit_progress("init", "バックエンドを初期化中", 0.72)

log_cfg = config.get("logging") or {}
log_level = log_cfg.get("level", "INFO")
if config.get("debug", "enabled", default=False):
    log_level = "DEBUG"
setup_logging(
    level=log_level,
    log_dir=log_cfg.get("dir", "~/.seam/logs/"),
    max_size_mb=log_cfg.get("max_size_mb", 50),
    backup_count=log_cfg.get("backup_count", 3),
)
_start_parent_watchdog()

app = FastAPI(title="Seam Backend", version="0.1.0-beta.1")


def _build_allow_origins() -> list[str]:
    server_cfg = config.get("server") or {}
    extra = normalize_origin_list(
        server_cfg.get("allowed_origins", []) if isinstance(server_cfg, dict) else [],
    )
    return list(dict.fromkeys([*DEFAULT_LOCAL_ORIGINS, *extra]))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_build_allow_origins(),
    allow_origin_regex=LOCAL_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _origin_guard_middleware(request: Request, call_next):
    server_cfg = config.get("server") or {}
    allow_remote = bool(server_cfg.get("allow_remote_clients", False)) if isinstance(server_cfg, dict) else False
    path = request.url.path

    if should_enforce_loopback_for_request_path(path):
        client_host = request.client.host if request.client else None
        if not allow_remote and not is_loopback_client_host(client_host):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "code": "REMOTE_CLIENT_NOT_ALLOWED",
                        "message": "remote clients are disabled",
                    }
                },
            )

    if should_enforce_origin_for_request(request.method, path):
        origin = request.headers.get("origin")
        extra_origins = normalize_origin_list(
            server_cfg.get("allowed_origins", []) if isinstance(server_cfg, dict) else [],
        )
        if not is_allowed_request_origin(origin, allowed_origins=extra_origins):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "code": "ORIGIN_NOT_ALLOWED",
                        "message": "origin is not allowed",
                    }
                },
            )
    return await call_next(request)

# Routers
# NOTE:
# ログ設定/環境変数調整後に import したいため、router import は意図的に遅延。
from src.api.projects import router as projects_router
from src.api.settings import router as settings_router
from src.api.minutes import router as minutes_router
from src.api.ws import router as ws_router
from src.api.devices import router as devices_router
from src.api.recording import router as recording_router
from src.api.debug import router as debug_router
from src.api.speakers import router as speakers_router
from src.api.summarize import router as summarize_router
from src.api.dictionary import router as dictionary_router
from src.api.util import router as util_router

app.include_router(projects_router)
app.include_router(settings_router)
app.include_router(minutes_router)
app.include_router(ws_router)
app.include_router(devices_router)
app.include_router(recording_router)
app.include_router(debug_router)
app.include_router(speakers_router)
app.include_router(summarize_router)
app.include_router(dictionary_router)
app.include_router(util_router)


@app.on_event("startup")
async def _preload_whisper() -> None:
    """必要時のみ起動時プリロード。既定は遅延ロード。"""
    if not config.get("whisper", "preload_on_startup", default=False):
        logger.info("Whisper preload skipped (preload_on_startup=false)")
        return
    emit_progress("whisper", "Whisper モデルをロード中", 0.78)
    from src.transcribe.streaming import preload_model
    model_name = config.get("whisper", "model", default="medium")
    preload_model(model_name)
    emit_progress("whisper", "Whisper モデルのロード完了", 0.84)


@app.on_event("startup")
async def _recover_summary_drafts() -> None:
    """前回のジョブが DB 反映前に落ちた場合の救済 (~/.seam/summary_drafts/*.md → DB)。"""
    emit_progress("recovery", "中断された要約ジョブを確認中", 0.86)
    try:
        from src.summarize.runner import recover_drafts

        recover_drafts()
    except Exception as e:
        logger.warning("summary draft recovery failed: %s", e)


@app.on_event("startup")
async def _recover_pending_sessions() -> None:
    """録音停止後の文字起こし / 保存途中で落ちたセッションを救済。

    NOTE:
    セッション復旧は長時間化しうる(長尺音声の再話者分離など)ため、
    起動シーケンスをブロックしないようバックグラウンドで実行する。
    """
    emit_progress("recovery", "未完了の録音セッションを確認中", 0.90)
    from src.api.recording import recover_pending_sessions_async

    async def _run() -> None:
        try:
            recovered = await recover_pending_sessions_async()
            if recovered:
                logger.info("startup background recovery completed: %d", recovered)
        except Exception as e:
            logger.warning("session recovery failed: %s", e)

    task = asyncio.create_task(_run(), name="startup-session-recovery")
    _startup_background_tasks.add(task)
    task.add_done_callback(_startup_background_tasks.discard)


@app.on_event("startup")
async def _start_summary_runner() -> None:
    """要約ジョブの非同期ワーカーを起動。WS broadcasterを注入。"""
    emit_progress("summary", "要約ワーカーを起動中", 0.94)
    try:
        from src.api.ws import ws_manager
        from src.summarize.runner import get_runner, set_broadcaster

        set_broadcaster(ws_manager.broadcast)
        runner = get_runner()
        await runner.start()
    except Exception as e:
        logger.warning("summary runner start failed: %s", e)


@app.on_event("startup")
async def _autostart_ollama() -> None:
    """ollama provider 選択中 + auto_start=true なら、起動時に Ollama を立ち上げる。

    ベストエフォート。失敗してもアプリ起動は続行。実際の要約ジョブで再試行される。
    """
    try:
        provider = str(config.get("minutes_ai", "provider", default="ollama"))
        auto_start = bool(config.get("ollama", "auto_start", default=True))
        if provider != "ollama" or not auto_start:
            emit_progress("ready", "起動完了", 1.0)
            return
        emit_progress("ollama", "Ollama を起動中", 0.97)
        from src.summarize.ollama_runtime import ensure_ollama_running

        base_url = str(
            config.get("minutes_ai", "ollama", "base_url",
                       default="http://localhost:11434"),
        )
        ok = await ensure_ollama_running(
            base_url=base_url, auto_start=True, timeout_sec=10.0,
        )
        if ok:
            logger.info("Ollama is ready (auto-start succeeded or already up)")
        else:
            logger.info(
                "Ollama auto-start skipped or failed; will retry on first job",
            )
    except Exception as e:
        logger.warning("Ollama auto-start hook error: %s", e)
    finally:
        emit_progress("ready", "起動完了", 1.0)


@app.on_event("shutdown")
async def _stop_summary_runner() -> None:
    try:
        from src.summarize.runner import get_runner

        runner = get_runner()
        await runner.shutdown()
    except Exception as e:
        logger.warning("summary runner shutdown failed: %s", e)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    server_cfg = config.get("server") or {}
    reload_enabled = bool(server_cfg.get("reload", config.get("debug", "enabled", default=False)))
    # Tauri アプリ起動時は reload を無効化する。
    # reload=True だと reloader/worker の二重プロセスになり、startup hook が重複して
    # モデルロードや状態管理が不安定になりやすい。
    if os.environ.get("SEAM_PARENT_PID"):
        if reload_enabled:
            logger.info("uvicorn reload disabled in app runtime (SEAM_PARENT_PID set)")
        reload_enabled = False
    uvicorn.run(
        "src.main:app",
        host=server_cfg.get("host", "127.0.0.1"),
        port=server_cfg.get("port", 18900),
        reload=reload_enabled,
    )


if __name__ == "__main__":
    main()
