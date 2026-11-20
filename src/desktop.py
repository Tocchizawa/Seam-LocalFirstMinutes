"""Entry point for the backend owned by the native macOS app."""
from __future__ import annotations

import fcntl
import json
import os
import socket
from pathlib import Path

from src.startup_progress import emit


def main() -> None:
    emit("init", "設定と作業環境を読み込み中", 0.60)
    import uvicorn

    from src.config import APP_DIR, config

    status = Path(os.environ["SEAM_DESKTOP_STATUS"])

    def publish(**fields: object) -> None:
        temporary = status.with_suffix(".tmp")
        temporary.write_text(json.dumps(fields), encoding="utf-8")
        temporary.replace(status)

    runtime = APP_DIR / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "desktop-backend.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            publish(error="同じデータを使う録音サービスが起動中です。")
            return
        try:
            port = int(os.environ.get("SEAM_DESKTOP_PORT") or config.get("server", "port", default=18900))
            if not 0 <= port <= 65535:
                raise ValueError
        except (ValueError, TypeError):
            publish(error="設定のサーバーポート番号が不正です。")
            return
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                publish(error=f"ポート {port} を開けませんでした。従来版のSeamや同じポートを使うサービスを確認してください。")
                return
            from src.main import app

            publish(port=listener.getsockname()[1])
            uvicorn.Server(uvicorn.Config(app, timeout_graceful_shutdown=5)).run(sockets=[listener])


if __name__ == "__main__":
    main()
