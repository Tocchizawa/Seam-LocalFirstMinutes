from __future__ import annotations

import os
import shlex
import shutil
from typing import Any


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
    launcher_command = str(cfg.get("launcher_command", "") or "").strip()
    if launcher_command:
        shell_path = str(cfg.get("launcher_shell", "/bin/zsh") or "/bin/zsh").strip()
        shell_flag = "-ic" if bool(cfg.get("launcher_interactive", True)) else "-lc"
        joined = shlex.join(command_args)
        shell_cmd = f"{launcher_command} {joined}".strip()
        return [shell_path, shell_flag, shell_cmd], launcher_command

    binary_path = str(cfg.get("binary_path", default_binary) or default_binary).strip()
    binary = resolve_binary(binary_path)
    if binary is None:
        return None, binary_path
    return [binary, *command_args], binary_path


def is_shell_command_not_found(text: str) -> bool:
    low = (text or "").lower()
    return "command not found" in low or "not found" in low
