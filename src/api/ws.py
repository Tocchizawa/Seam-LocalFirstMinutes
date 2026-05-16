from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.config import config
from src.security import (
    is_allowed_request_origin,
    is_loopback_client_host,
    normalize_origin_list,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _to_jsonable(obj):
    """numpy 型などを JSON シリアライズ可能な型に変換する。"""
    # numpy が無いケースに備え遅延 import
    try:
        import numpy as np  # type: ignore
        np_floating = np.floating
        np_integer = np.integer
        np_ndarray = np.ndarray
    except ImportError:
        np_floating = ()
        np_integer = ()
        np_ndarray = ()

    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if np_floating and isinstance(obj, np_floating):
        return float(obj)
    if np_integer and isinstance(obj, np_integer):
        return int(obj)
    if np_ndarray and isinstance(obj, np_ndarray):
        return obj.tolist()
    return obj


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> bool:
        server_cfg = config.get("server") or {}
        allow_remote = bool(server_cfg.get("allow_remote_clients", False)) if isinstance(server_cfg, dict) else False
        client_host = ws.client.host if ws.client else None
        if not allow_remote and not is_loopback_client_host(client_host):
            logger.warning("WS rejected by remote client host: %s", client_host)
            await ws.close(code=1008, reason="remote client is not allowed")
            return False

        origin = ws.headers.get("origin")
        extra_origins = normalize_origin_list(
            server_cfg.get("allowed_origins", []) if isinstance(server_cfg, dict) else [],
        )
        if not is_allowed_request_origin(origin, allowed_origins=extra_origins):
            logger.warning("WS rejected by origin: %s", origin)
            await ws.close(code=1008, reason="origin not allowed")
            return False
        await ws.accept()
        self.connections.append(ws)
        logger.info("WS connected (total=%d)", len(self.connections))
        return True

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.connections:
            self.connections.remove(ws)
        logger.info("WS disconnected (total=%d)", len(self.connections))

    async def broadcast(self, message: dict) -> None:
        msg_type = message.get("type", "?")
        # 型サニタイズ(numpy 値などを JSON 互換に)
        safe_message = _to_jsonable(message)
        sent = 0
        for ws in self.connections[:]:
            try:
                await ws.send_json(safe_message)
                sent += 1
            except WebSocketDisconnect:
                # 正常な切断: connections から削除
                if ws in self.connections:
                    self.connections.remove(ws)
                logger.info("WS disconnected during send (type=%s, total=%d)",
                            msg_type, len(self.connections))
            except Exception as e:
                # 送信エラー(JSON 化失敗など)は接続を削除しない — 単に1メッセージスキップ
                logger.warning("WS send error (id=%s, type=%s): %s",
                               id(ws), msg_type, e)
        if msg_type == "transcript_chunk":
            logger.info("→ broadcast transcript_chunk to %d/%d clients",
                        sent, len(self.connections))


ws_manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    accepted = await ws_manager.connect(ws)
    if not accepted:
        return
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
