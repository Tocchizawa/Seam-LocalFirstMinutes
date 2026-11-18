from __future__ import annotations

import os
import subprocess
import shlex
import shutil
from typing import Any

from .base import SummaryErrorCode

DEFAULT_CLI_CONNECT_TIMEOUT_SEC = 12.0

_PATH_MARKER = "__SEAM_REFRESHED_PATH__="
_SHELL_CACHE_CLEAR = "hash -r 2>/dev/null || true; rehash 2>/dev/null || true;"
_COMMON_CLI_PATHS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "~/.local/bin",
    "~/.npm-global/bin",
    "~/.volta/bin",
    "~/.bun/bin",
    "~/.deno/bin",
    "~/.cargo/bin",
)


def normalize_extra_args(raw: Any) -> list[str]:
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            return [x for x in shlex.split(s) if x]
        except Exception:
            return [s]
    return []


def clamp_connect_timeout(raw: Any, *, default: float = DEFAULT_CLI_CONNECT_TIMEOUT_SEC) -> float:
    try:
        value = float(raw)
    except Exception:
        value = default
    return max(3.0, min(60.0, value))


def _split_path(value: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in (value or "").split(os.pathsep):
        path = os.path.expanduser(item.strip())
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _read_login_shell_path(shell_path: str) -> str:
    if not shell_path or not os.path.exists(shell_path):
        return ""
    if any(ch in shell_path for ch in ("\n", "\r", "\x00")):
        return ""
    try:
        proc = subprocess.run(
            [
                shell_path,
                "-lic",
                f'printf "%s\\n" "{_PATH_MARKER}$PATH"',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(_PATH_MARKER):
            return line[len(_PATH_MARKER):].strip()
    return ""


def refresh_cli_environment(cfg: dict[str, Any] | None = None) -> str:
    """Update PATH from the user's login shell before resolving CLI binaries."""
    cfg = cfg or {}
    shell_path = str(
        cfg.get("launcher_shell")
        or os.environ.get("SHELL")
        or "/bin/zsh"
    ).strip()

    merged: list[str] = []
    seen: set[str] = set()

    def add_many(paths: list[str]) -> None:
        for path in paths:
            if path and path not in seen:
                seen.add(path)
                merged.append(path)

    shell_path_value = _read_login_shell_path(shell_path)
    add_many(_split_path(shell_path_value))
    add_many(_split_path(os.pathsep.join(_COMMON_CLI_PATHS)))
    add_many(_split_path(os.environ.get("PATH", "")))

    if merged:
        os.environ["PATH"] = os.pathsep.join(merged)
    return os.environ.get("PATH", "")


def resolve_binary(path: str) -> str | None:
    if not path:
        return None
    if "/" in path:
        return path if os.access(path, os.X_OK) else None
    return shutil.which(path)


def build_command_argv(
    cfg: dict[str, Any],
    *,
    default_binary: str,
    command_args: list[str],
) -> tuple[list[str] | None, str]:
    refresh_cli_environment(cfg)
    launcher_command = str(cfg.get("launcher_command", "") or "").strip()
    if launcher_command:
        shell_path = str(cfg.get("launcher_shell", "/bin/zsh") or "/bin/zsh").strip()
        shell_flag = "-ic" if bool(cfg.get("launcher_interactive", True)) else "-lc"
        joined = shlex.join(command_args)
        shell_cmd = f"{_SHELL_CACHE_CLEAR} {launcher_command} {joined}".strip()
        return [shell_path, shell_flag, shell_cmd], launcher_command

    binary_path = str(cfg.get("binary_path", default_binary) or default_binary).strip()
    binary = resolve_binary(binary_path)
    if binary is None:
        return None, binary_path
    return [binary, *command_args], binary_path


def is_shell_command_not_found(text: str) -> bool:
    low = (text or "").lower()
    return "command not found" in low or "not found" in low


def strip_cli_error(text: str, *, limit: int = 300) -> str:
    cleaned = " ".join((text or "").strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def classify_cli_error_code(text: str) -> SummaryErrorCode:
    low = (text or "").lower()
    auth_markers = (
        "authentication failed",
        "not authenticated",
        "not logged in",
        "login required",
        "please log in",
        "unauthorized",
        "invalid api key",
        "401",
        "403",
        "oauth",
    )
    if any(marker in low for marker in auth_markers):
        return SummaryErrorCode.AUTH_FAILED

    offline_markers = (
        "failed to lookup address information",
        "could not resolve host",
        "temporary failure in name resolution",
        "connection refused",
        "connection reset",
        "connection aborted",
        "connection error",
        "network is unreachable",
        "operation timed out",
        "timed out",
        "timeout",
        "tls handshake",
        "offline",
    )
    if any(marker in low for marker in offline_markers):
        return SummaryErrorCode.OFFLINE

    return SummaryErrorCode.PROVIDER_DOWN
