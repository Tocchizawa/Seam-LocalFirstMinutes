"""Seam — セキュリティ強化ポイントの軽量テスト"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.project.manager import OUTPUT_DIR_SENTINEL, ProjectManager
from src.security import (
    is_allowed_request_origin,
    is_loopback_client_host,
    is_safe_session_id,
    resolve_existing_absolute_path,
    resolve_path_under_base,
    should_enforce_loopback_for_request_path,
    should_enforce_origin_for_request,
)

PASS = 0
FAIL = 0


def ok(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        print(f"  PASS: {name}")
        PASS += 1
    else:
        print(f"  FAIL: {name}")
        FAIL += 1


print("\n[1] Origin validation")
ok("localhost origin is allowed", is_allowed_request_origin("http://localhost:1420"))
ok("127.0.0.1 origin is allowed", is_allowed_request_origin("http://127.0.0.1:3000"))
ok("tauri origin is allowed", is_allowed_request_origin("tauri://localhost"))
ok("remote origin is denied", not is_allowed_request_origin("https://evil.example.com"))
ok("null origin is denied", not is_allowed_request_origin("null"))


print("\n[2] session_id validation")
ok("normal id is allowed", is_safe_session_id("20260517_123456"))
ok("suffix id is allowed", is_safe_session_id("20260517_123456_abcd"))
ok("empty is denied", not is_safe_session_id(""))
ok("slash is denied", not is_safe_session_id("../etc/passwd"))
ok("space is denied", not is_safe_session_id("bad id"))


print("\n[3] absolute path resolution")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    target = root / "sample.txt"
    target.write_text("ok", encoding="utf-8")
    resolved = resolve_existing_absolute_path(str(target))
    ok("existing absolute path resolves", resolved == target.resolve())
    try:
        resolve_existing_absolute_path("relative/path.txt")
        ok("relative path should fail", False)
    except ValueError:
        ok("relative path should fail", True)


print("\n[4] base directory path guard")
with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    nested = resolve_path_under_base(base, "nested/file.txt")
    ok("nested relative path is allowed", nested == (base / "nested" / "file.txt").resolve())
    try:
        resolve_path_under_base(base, "../../etc/passwd")
        ok("path traversal should fail", False)
    except ValueError:
        ok("path traversal should fail", True)


print("\n[5] mutating request origin scope")
ok("GET /api is excluded", not should_enforce_origin_for_request("GET", "/api/settings"))
ok("POST /api is enforced", should_enforce_origin_for_request("POST", "/api/settings"))
ok("DELETE /api is enforced", should_enforce_origin_for_request("DELETE", "/api/projects/x"))
ok("POST non-api path is excluded", not should_enforce_origin_for_request("POST", "/health"))


print("\n[6] loopback client host")
ok("127.0.0.1 is loopback", is_loopback_client_host("127.0.0.1"))
ok("::1 is loopback", is_loopback_client_host("::1"))
ok("localhost is loopback", is_loopback_client_host("localhost"))
ok("private LAN ip is not loopback", not is_loopback_client_host("192.168.0.10"))
ok("public ip is not loopback", not is_loopback_client_host("8.8.8.8"))


print("\n[7] loopback path scope")
ok("/api path is guarded", should_enforce_loopback_for_request_path("/api/settings"))
ok("non-api path is not guarded", not should_enforce_loopback_for_request_path("/health"))


print("\n[8] output_dir delete guard")
pm = ProjectManager.__new__(ProjectManager)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    output_dir = root / "safe-output-dir"
    output_dir.mkdir(parents=True, exist_ok=True)
    can, reason = pm._can_delete_output_dir(output_dir)
    ok("missing sentinel is denied", not can and "missing sentinel" in reason)

    (output_dir / OUTPUT_DIR_SENTINEL).touch(exist_ok=True)
    can, _ = pm._can_delete_output_dir(output_dir)
    ok("marked output dir is allowed", can)

    symlink_path = root / "safe-output-link"
    symlink_path.symlink_to(output_dir, target_is_directory=True)
    can, reason = pm._can_delete_output_dir(symlink_path)
    ok("symlink is denied", not can and "symlink" in reason)


print("\n=========================================")
print(f"  結果: {PASS} passed, {FAIL} failed")
print("=========================================")
sys.exit(0 if FAIL == 0 else 1)
