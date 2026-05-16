from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_LOCAL_ORIGINS: tuple[str, ...] = (
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
)

LOCAL_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\]|tauri\.localhost)(:\d+)?$"

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "tauri.localhost"}
MUTATING_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _normalize_origin(value: object) -> str:
    return str(value or "").strip().rstrip("/").lower()


def normalize_origin_list(raw: object) -> list[str]:
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in raw:
        s = _normalize_origin(item)
        if s:
            out.append(s)
    return out


def is_allowed_request_origin(
    origin: str | None,
    *,
    allowed_origins: list[str] | None = None,
) -> bool:
    origin_norm = _normalize_origin(origin)
    # Origin が無いクライアント(curl/ローカルスクリプト等)は許可。
    if not origin_norm:
        return True
    if origin_norm == "null":
        return False

    allowed = {_normalize_origin(x) for x in (allowed_origins or []) if _normalize_origin(x)}
    if origin_norm in allowed:
        return True

    parsed = urlparse(origin_norm)
    host = (parsed.hostname or "").strip().lower()
    if parsed.scheme == "tauri" and host == "localhost":
        return True
    if parsed.scheme in {"http", "https"} and host in _LOCAL_HOSTS:
        return True
    return False


def is_safe_session_id(value: str) -> bool:
    session_id = str(value or "").strip()
    if not session_id:
        return False
    if session_id in {".", ".."}:
        return False
    return bool(SESSION_ID_RE.fullmatch(session_id))


def resolve_existing_absolute_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ValueError("absolute path is required")
    return path.resolve(strict=True)


def resolve_path_under_base(base_dir: Path, relative_path: str) -> Path:
    rel = Path(str(relative_path or "").strip())
    if rel.is_absolute():
        raise ValueError("absolute path is not allowed")
    base = base_dir.expanduser().resolve()
    resolved = (base / rel).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError("path escapes base directory")
    return resolved


def should_enforce_origin_for_request(method: str, path: str) -> bool:
    m = str(method or "").upper()
    p = str(path or "")
    return m in MUTATING_HTTP_METHODS and p.startswith("/api/")


def is_loopback_client_host(host: str | None) -> bool:
    value = str(host or "").strip().strip("[]").lower()
    if value in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def should_enforce_loopback_for_request_path(path: str) -> bool:
    p = str(path or "")
    return p.startswith("/api/")
