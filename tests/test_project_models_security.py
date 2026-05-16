"""Seam — Project model path validation tests"""
from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.project.models import ProjectCreate, ProjectUpdate

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


print("\n[1] create model accepts normalized absolute paths")
p = ProjectCreate(
    name="  test project  ",
    repo_path="  /tmp/seam-repo  ",
    doc_dirs=["/tmp/docs-a", "/tmp/docs-a", "/tmp/docs-b"],
    output_dir="~/seam-output",
)
ok("name is trimmed", p.name == "test project")
ok("repo path is trimmed", p.repo_path == "/tmp/seam-repo")
ok("doc dirs are deduplicated", p.doc_dirs == ["/tmp/docs-a", "/tmp/docs-b"])
ok("output_dir keeps absolute form", p.output_dir.startswith("/"))


print("\n[2] create model rejects invalid paths")
try:
    ProjectCreate(name="x", output_dir="relative/path")
    ok("relative output_dir should fail", False)
except ValidationError:
    ok("relative output_dir should fail", True)

try:
    ProjectCreate(name="x", output_dir="/tmp/out", repo_path="../repo")
    ok("relative repo_path should fail", False)
except ValidationError:
    ok("relative repo_path should fail", True)

try:
    ProjectCreate(name="x", output_dir="/tmp/out", doc_dirs=["/tmp/ok", "bad/dir"])
    ok("relative doc_dirs should fail", False)
except ValidationError:
    ok("relative doc_dirs should fail", True)


print("\n[3] update model validation")
u = ProjectUpdate(name="  new name  ", output_dir="/tmp/new-out", repo_path="")
ok("update name is trimmed", u.name == "new name")
ok("empty repo_path becomes None", u.repo_path is None)

try:
    ProjectUpdate(output_dir=".")
    ok("relative update output_dir should fail", False)
except ValidationError:
    ok("relative update output_dir should fail", True)

try:
    ProjectUpdate(name="   ")
    ok("blank update name should fail", False)
except ValidationError:
    ok("blank update name should fail", True)

print("\n=========================================")
print(f"  結果: {PASS} passed, {FAIL} failed")
print("=========================================")
sys.exit(0 if FAIL == 0 else 1)
