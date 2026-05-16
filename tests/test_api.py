"""Seam — 包括的 API テスト (Python)"""
import os
import shutil
import sys

import httpx

BASE = "http://127.0.0.1:18900"
PASS = 0
FAIL = 0


def assert_status(desc: str, expected: int, response: httpx.Response):
    global PASS, FAIL
    if response.status_code == expected:
        print(f"  PASS: {desc} (HTTP {response.status_code})")
        PASS += 1
    else:
        print(f"  FAIL: {desc} (expected {expected}, got {response.status_code})")
        print(f"    body: {response.text[:200]}")
        FAIL += 1


def assert_json(desc: str, data, key_path: str, expected):
    global PASS, FAIL
    keys = key_path.split(".")
    current = data
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k)
        elif isinstance(current, list) and k.isdigit():
            current = current[int(k)]
        else:
            current = None
            break
    if current == expected:
        print(f"  PASS: {desc} ({key_path} == {expected!r})")
        PASS += 1
    else:
        print(f"  FAIL: {desc} ({key_path} == {current!r}, expected {expected!r})")
        FAIL += 1


def assert_contains(desc: str, text: str, pattern: str):
    global PASS, FAIL
    if pattern in text:
        print(f"  PASS: {desc}")
        PASS += 1
    else:
        print(f"  FAIL: {desc} (missing '{pattern}')")
        print(f"    body: {text[:200]}")
        FAIL += 1


def assert_true(desc: str, condition: bool):
    global PASS, FAIL
    if condition:
        print(f"  PASS: {desc}")
        PASS += 1
    else:
        print(f"  FAIL: {desc}")
        FAIL += 1


def assert_len(desc: str, data: list, expected: int):
    global PASS, FAIL
    if len(data) == expected:
        print(f"  PASS: {desc} (len={expected})")
        PASS += 1
    else:
        print(f"  FAIL: {desc} (len={len(data)}, expected {expected})")
        FAIL += 1


client = httpx.Client(base_url=BASE, timeout=10)

print("=========================================")
print("  Seam API テスト")
print("=========================================")

# ─── 1. Health ───
print("\n[1] Health Check")
r = client.get("/health")
assert_status("GET /health", 200, r)
assert_json("status is ok", r.json(), "status", "ok")

# ─── 2. Settings ───
print("\n[2] Settings — デフォルト値")
r = client.get("/api/settings")
assert_status("GET /api/settings", 200, r)
s = r.json()
assert_json("whisper.model default", s, "whisper.model", "medium")
assert_json("whisper.language default", s, "whisper.language", "ja")
assert_json("whisper.device default", s, "whisper.device", "auto")
assert_json("whisper.streaming_chunk_sec", s, "whisper.streaming_chunk_sec", 5)
assert_json("ollama.context_model", s, "ollama.context_model", "qwen3:8b")
assert_json("ollama.minutes_model", s, "ollama.minutes_model", "qwen3:8b")
assert_json("ollama.base_url", s, "ollama.base_url", "http://localhost:11434")
assert_json("server.host", s, "server.host", "127.0.0.1")
assert_json("server.port", s, "server.port", 18900)
assert_json("recording.sample_rate", s, "recording.sample_rate", 16000)
assert_json("agent.max_steps", s, "agent.max_steps", 15)
assert_json("agent.max_context_tokens", s, "agent.max_context_tokens", 28000)
assert_json("logging.level", s, "logging.level", "INFO")
assert_json("setup.completed", s, "setup.completed", False)

print("\n[3] Settings — 更新")
r = client.put("/api/settings", json={"whisper": {"model": "small"}})
assert_status("PUT /api/settings", 200, r)
s = r.json()
assert_json("updated whisper.model", s, "whisper.model", "small")
assert_json("preserved whisper.language", s, "whisper.language", "ja")
assert_json("preserved ollama.context_model", s, "ollama.context_model", "qwen3:8b")
assert_json("preserved server.port", s, "server.port", 18900)

# 部分更新（nested deep merge）
r = client.put("/api/settings", json={"ollama": {"context_model": "qwen3:4b"}})
assert_status("PUT nested update", 200, r)
s = r.json()
assert_json("updated ollama.context_model", s, "ollama.context_model", "qwen3:4b")
assert_json("preserved ollama.base_url", s, "ollama.base_url", "http://localhost:11434")
assert_json("still updated whisper.model", s, "whisper.model", "small")

# Restore
client.put("/api/settings", json={
    "whisper": {"model": "medium"},
    "ollama": {"context_model": "qwen3:8b"},
})

# ─── 3. Projects — CRUD ───
print("\n[4] Projects — 初期状態 (空)")
r = client.get("/api/projects")
assert_status("GET /api/projects", 200, r)
assert_len("empty list", r.json(), 0)

print("\n[5] Projects — 作成 (プロジェクト1)")
r = client.post("/api/projects", json={
    "name": "えがいて",
    "repo_path": "/tmp/seam-test-repo",
    "output_dir": "/tmp/seam-test-output1",
    "doc_dirs": ["/tmp/seam-test-docs"],
    "members": [{"name": "とちざわ", "role": "リード"}],
    "glossary": ["Supabase: BaaS", "OG画像: プレビュー"],
})
assert_status("POST /api/projects", 201, r)
p1 = r.json()
assert_json("project name", p1, "name", "えがいて")
assert_json("repo_path", p1, "repo_path", "/tmp/seam-test-repo")
assert_json("output_dir", p1, "output_dir", "/tmp/seam-test-output1")
assert_len("doc_dirs", p1["doc_dirs"], 1)
assert_len("members", p1["members"], 1)
assert_json("member name", p1["members"][0], "name", "とちざわ")
assert_json("member role", p1["members"][0], "role", "リード")
assert_len("glossary", p1["glossary"], 2)
assert_true("id is non-empty string", isinstance(p1["id"], str) and len(p1["id"]) > 0)
assert_true("created_at is set", len(p1["created_at"]) > 0)
assert_true("updated_at is set", len(p1["updated_at"]) > 0)
assert_true("output_dir auto-created", os.path.isdir("/tmp/seam-test-output1"))
P1_ID = p1["id"]

print("\n[6] Projects — 作成 (プロジェクト2, minimal)")
r = client.post("/api/projects", json={
    "name": "みらもる",
    "output_dir": "/tmp/seam-test-output2",
})
assert_status("POST minimal project", 201, r)
p2 = r.json()
assert_json("project name", p2, "name", "みらもる")
assert_json("repo_path is null", p2, "repo_path", None)
assert_len("doc_dirs empty", p2["doc_dirs"], 0)
assert_len("members empty", p2["members"], 0)
assert_len("glossary empty", p2["glossary"], 0)
P2_ID = p2["id"]

print("\n[7] Projects — 一覧 (2件)")
r = client.get("/api/projects")
assert_status("GET /api/projects", 200, r)
assert_len("2 projects", r.json(), 2)

print("\n[8] Projects — 個別取得")
r = client.get(f"/api/projects/{P1_ID}")
assert_status("GET /api/projects/{id}", 200, r)
assert_json("correct project", r.json(), "name", "えがいて")

print("\n[9] Projects — 存在しないID取得")
r = client.get("/api/projects/nonexistent")
assert_status("GET nonexistent", 404, r)

print("\n[10] Projects — 更新")
r = client.put(f"/api/projects/{P1_ID}", json={
    "name": "えがいて v2",
    "glossary": ["Supabase: BaaS", "OG画像: プレビュー", "新用語"],
})
assert_status("PUT /api/projects/{id}", 200, r)
p1u = r.json()
assert_json("updated name", p1u, "name", "えがいて v2")
assert_len("updated glossary", p1u["glossary"], 3)
assert_json("preserved repo_path", p1u, "repo_path", "/tmp/seam-test-repo")
assert_json("preserved output_dir", p1u, "output_dir", "/tmp/seam-test-output1")
assert_len("preserved doc_dirs", p1u["doc_dirs"], 1)
assert_len("preserved members", p1u["members"], 1)

print("\n[11] Projects — 部分更新 (名前のみ)")
r = client.put(f"/api/projects/{P1_ID}", json={"name": "えがいて v3"})
assert_status("PUT partial update", 200, r)
assert_json("partial updated name", r.json(), "name", "えがいて v3")
assert_true("glossary preserved", r.json()["glossary"][2] == "新用語")

print("\n[12] Projects — 存在しないID更新")
r = client.put("/api/projects/nonexistent", json={"name": "test"})
assert_status("PUT nonexistent", 404, r)

print("\n[13] Projects — 削除 (output保持)")
r = client.delete(f"/api/projects/{P2_ID}")
assert_status("DELETE /api/projects/{id}", 200, r)
assert_true("output_dir preserved", os.path.isdir("/tmp/seam-test-output2"))

r = client.get("/api/projects")
assert_len("1 project after delete", r.json(), 1)

print("\n[14] Projects — 削除 (output削除)")
r = client.delete(f"/api/projects/{P1_ID}?delete_output=true")
assert_status("DELETE with delete_output", 200, r)
assert_true("output_dir deleted", not os.path.isdir("/tmp/seam-test-output1"))

r = client.get("/api/projects")
assert_len("0 projects after all deleted", r.json(), 0)

print("\n[15] Projects — 存在しないID削除")
r = client.delete("/api/projects/nonexistent")
assert_status("DELETE nonexistent", 404, r)

print("\n[16] Projects — 二重削除")
r = client.delete(f"/api/projects/{P1_ID}")
assert_status("DELETE already deleted", 404, r)

# ─── 4. Validation ───
print("\n[17] Validation — 不正JSON")
r = client.post("/api/projects", content=b"invalid", headers={"Content-Type": "application/json"})
assert_status("POST invalid JSON", 422, r)

print("\n[18] Validation — 必須フィールド欠落 (output_dir)")
r = client.post("/api/projects", json={"name": "test"})
assert_status("POST missing output_dir", 422, r)

print("\n[19] Validation — 必須フィールド欠落 (name)")
r = client.post("/api/projects", json={"output_dir": "/tmp/test"})
assert_status("POST missing name", 422, r)

print("\n[20] Validation — 空body")
r = client.post("/api/projects", content=b"{}", headers={"Content-Type": "application/json"})
assert_status("POST empty body", 422, r)

# ─── 5. Minutes (DB直接テスト) ───
print("\n[21] Minutes — 空一覧")
r = client.get("/api/minutes")
assert_status("GET /api/minutes", 200, r)
assert_len("empty minutes", r.json(), 0)

print("\n[22] Minutes — プロジェクトフィルター (空)")
r = client.get("/api/minutes?project=some_project")
assert_status("GET /api/minutes?project=...", 200, r)
assert_len("filtered empty", r.json(), 0)

print("\n[23] Minutes — ページネーション")
r = client.get("/api/minutes?limit=5&offset=0")
assert_status("GET with pagination", 200, r)

print("\n[24] Minutes — 存在しないID取得")
r = client.get("/api/minutes/nonexistent")
assert_status("GET nonexistent minutes", 404, r)

print("\n[25] Minutes — 存在しないID transcript")
r = client.get("/api/minutes/nonexistent/transcript")
assert_status("GET nonexistent transcript", 404, r)

print("\n[26] Minutes — 存在しないID project再割り当て")
r = client.put("/api/minutes/nonexistent/project", json={"project_id": "test"})
assert_status("PUT nonexistent project reassign", 404, r)

print("\n[28] Minutes — project再割り当て (project_id欠落)")
r = client.put("/api/minutes/nonexistent/project", json={})
assert_status("PUT missing project_id", 400, r)

print("\n[29] Minutes — 存在しないID削除")
r = client.delete("/api/minutes/nonexistent")
assert_status("DELETE nonexistent minutes", 404, r)

print("\n[30] Minutes — 検索")
r = client.get("/api/minutes/search?q=テスト")
assert_status("GET search", 200, r)
assert_len("search empty", r.json(), 0)

print("\n[31] Minutes — 検索 (q欠落)")
r = client.get("/api/minutes/search")
assert_status("GET search missing q", 422, r)

# ─── 6. DB直接Insert + 検証 ───
print("\n[32] DB — 議事録の直接INSERT + API検証")
# Use the DB module directly via API isn't possible,
# so we test the DB layer through a mini Python script
import subprocess
result = subprocess.run(
    [sys.executable, "-c", """
import json, sys
sys.path.insert(0, '.')
from src.storage.db import Database, build_transcript_text

db = Database()
transcript = [
    {"start": 0.0, "end": 3.5, "text": "タグ機能の進捗について"},
    {"start": 3.8, "end": 7.2, "text": "CRUDのAPIは完了しました"},
    {"start": 7.5, "end": 12.0, "text": "フロントのUIはどうですか"},
]
data = {
    "id": "test-minutes-001",
    "session_id": "20260404_100000_abc123",
    "project_id": "test-project",
    "title": "テスト定例MTG",
    "date": "2026-04-04",
    "started_at": "2026-04-04T10:00:00",
    "duration_sec": 3600,
    "transcript": transcript,
    "summary": "# 議事録\\n\\nタグ機能の進捗を確認した。",
    "whisper_model": "medium",
    "llm_model": "qwen3:8b",
    "created_at": "2026-04-04T11:00:00",
    "updated_at": "2026-04-04T11:00:00",
}
db.insert_minutes(data)

# Verify transcript_text was generated
m = db.get_minutes("test-minutes-001")
assert m is not None, "minutes not found"
assert "transcript_text" in m, "transcript_text missing"
assert "タグ機能の進捗について" in m["transcript_text"], f"transcript_text content wrong: {m['transcript_text']}"
print("DB INSERT + transcript_text: OK")

# Verify JSON fields parsed
assert isinstance(m["transcript"], list), f"transcript not list: {type(m['transcript'])}"
print("DB JSON parse: OK")

# Insert second minutes for search test
data2 = data.copy()
data2["id"] = "test-minutes-002"
data2["session_id"] = "20260404_140000_def456"
data2["project_id"] = "test-project-2"
data2["title"] = "設計レビュー"
data2["date"] = "2026-04-03"
data2["transcript"] = [{"start": 0, "end": 5, "text": "Supabaseの設計を確認"}]
data2["summary"] = "Supabase設計レビュー"
db.insert_minutes(data2)
print("DB second INSERT: OK")

db.close()
"""],
    capture_output=True, text=True
)
if result.returncode == 0:
    for line in result.stdout.strip().split("\n"):
        assert_true(line, True)
else:
    print("  FAIL: DB test failed")
    print(f"    stderr: {result.stderr}")
    FAIL += 1

print("\n[33] Minutes — API で取得")
r = client.get("/api/minutes/test-minutes-001")
assert_status("GET inserted minutes", 200, r)
m = r.json()
assert_json("title", m, "title", "テスト定例MTG")
assert_true("transcript is list", isinstance(m["transcript"], list))
assert_true("transcript has 3 entries", len(m["transcript"]) == 3)

print("\n[34] Minutes — transcript 取得")
r = client.get("/api/minutes/test-minutes-001/transcript")
assert_status("GET transcript", 200, r)
t = r.json()
assert_true("transcript key exists", "transcript" in t)

print("\n[35] Minutes — 一覧 (2件)")
r = client.get("/api/minutes")
assert_len("2 minutes total", r.json(), 2)

print("\n[36] Minutes — プロジェクトフィルター")
r = client.get("/api/minutes?project=test-project")
assert_len("1 minute for test-project", r.json(), 1)
r = client.get("/api/minutes?project=test-project-2")
assert_len("1 minute for test-project-2", r.json(), 1)
r = client.get("/api/minutes?project=nonexistent")
assert_len("0 for nonexistent project", r.json(), 0)

print("\n[37] Minutes — 日付降順")
r = client.get("/api/minutes")
dates = [m["date"] for m in r.json()]
assert_true("sorted desc by date", dates == sorted(dates, reverse=True))

print("\n[38] Minutes — ページネーション")
r = client.get("/api/minutes?limit=1&offset=0")
assert_len("limit=1", r.json(), 1)
r = client.get("/api/minutes?limit=1&offset=1")
assert_len("offset=1", r.json(), 1)
r = client.get("/api/minutes?limit=1&offset=2")
assert_len("offset beyond", r.json(), 0)

print("\n[39] Minutes — FTS5 検索")
r = client.get("/api/minutes/search?q=タグ機能")
assert_status("search タグ機能", 200, r)
results = r.json()
assert_len("search found 1", results, 1)
assert_json("search result title", results[0], "title", "テスト定例MTG")

r = client.get("/api/minutes/search?q=Supabase")
results = r.json()
assert_len("search Supabase", results, 1)
assert_json("search Supabase title", results[0], "title", "設計レビュー")

r = client.get("/api/minutes/search?q=存在しないワード")
assert_len("search no match", r.json(), 0)

print("\n[40] Minutes — FTS5 プロジェクトフィルター付き検索")
r = client.get("/api/minutes/search?q=タグ機能&project=test-project")
assert_len("search with correct project", r.json(), 1)
r = client.get("/api/minutes/search?q=タグ機能&project=test-project-2")
assert_len("search with wrong project", r.json(), 0)

print("\n[41] Minutes — project 再割り当て")
r = client.put("/api/minutes/test-minutes-001/project", json={"project_id": "new-project"})
assert_status("PUT project reassign", 200, r)
r = client.get("/api/minutes/test-minutes-001")
assert_json("project reassigned", r.json(), "project_id", "new-project")

# Old project should now have 0
r = client.get("/api/minutes?project=test-project")
assert_len("old project empty", r.json(), 0)
r = client.get("/api/minutes?project=new-project")
assert_len("new project has 1", r.json(), 1)

print("\n[43] Minutes — 削除")
r = client.delete("/api/minutes/test-minutes-001")
assert_status("DELETE minutes", 200, r)
r = client.get("/api/minutes/test-minutes-001")
assert_status("GET deleted minutes", 404, r)
r = client.get("/api/minutes")
assert_len("1 remaining", r.json(), 1)

print("\n[44] Minutes — 二重削除")
r = client.delete("/api/minutes/test-minutes-001")
assert_status("DELETE already deleted", 404, r)

# Clean up second
client.delete("/api/minutes/test-minutes-002")

# ─── 7. Edge cases ───
print("\n[45] Edge — 日本語プロジェクト名")
r = client.post("/api/projects", json={
    "name": "超長いプロジェクト名テスト用あいうえおかきくけこ",
    "output_dir": "/tmp/seam-test-long",
})
assert_status("POST long Japanese name", 201, r)
long_id = r.json()["id"]
r = client.get(f"/api/projects/{long_id}")
assert_json("long name preserved", r.json(), "name", "超長いプロジェクト名テスト用あいうえおかきくけこ")
client.delete(f"/api/projects/{long_id}?delete_output=true")

print("\n[46] Edge — 特殊文字を含むプロジェクト")
r = client.post("/api/projects", json={
    "name": 'テスト "quotes" & <angle>',
    "output_dir": "/tmp/seam-test-special",
})
assert_status("POST special chars", 201, r)
spec_id = r.json()["id"]
r = client.get(f"/api/projects/{spec_id}")
assert_json("special chars preserved", r.json(), "name", 'テスト "quotes" & <angle>')
client.delete(f"/api/projects/{spec_id}?delete_output=true")

print("\n[47] Edge — 不正な Content-Type")
r = client.post("/api/projects", content=b"name=test", headers={"Content-Type": "text/plain"})
assert_status("POST text/plain", 422, r)

# ─── Summary ───
print("\n=========================================")
print(f"  結果: {PASS} passed, {FAIL} failed")
print("=========================================")

# Cleanup
shutil.rmtree("/tmp/seam-test-output2", ignore_errors=True)
shutil.rmtree("/tmp/seam-test-long", ignore_errors=True)
shutil.rmtree("/tmp/seam-test-special", ignore_errors=True)

sys.exit(1 if FAIL > 0 else 0)
