#!/bin/bash
# Seam — 包括的 API テスト
set -e

BASE="http://localhost:18900"
PASS=0
FAIL=0
TESTS=""

assert_status() {
    local desc="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS: $desc (HTTP $actual)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (expected $expected, got $actual)"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local desc="$1" body="$2" pattern="$3"
    if echo "$body" | grep -q "$pattern"; then
        echo "  PASS: $desc (contains '$pattern')"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (missing '$pattern')"
        echo "    body: $body"
        FAIL=$((FAIL + 1))
    fi
}

assert_not_contains() {
    local desc="$1" body="$2" pattern="$3"
    if echo "$body" | grep -q "$pattern"; then
        echo "  FAIL: $desc (should not contain '$pattern')"
        FAIL=$((FAIL + 1))
    else
        echo "  PASS: $desc (does not contain '$pattern')"
        PASS=$((PASS + 1))
    fi
}

echo "========================================="
echo "  Seam API テスト"
echo "========================================="

# ─── 1. Health ───
echo ""
echo "[1] Health Check"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/health")
assert_status "GET /health" 200 "$STATUS"
BODY=$(curl -s "$BASE/health")
assert_contains "health response" "$BODY" '"status":"ok"'

# ─── 2. Settings ───
echo ""
echo "[2] Settings"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/settings")
assert_status "GET /api/settings" 200 "$STATUS"
BODY=$(curl -s "$BASE/api/settings")
assert_contains "default whisper model" "$BODY" '"model": "medium"'
assert_contains "default ollama model" "$BODY" '"context_model": "qwen3:8b"'
assert_contains "server port" "$BODY" '"port": 18900'

# Update settings
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/settings" \
    -H "Content-Type: application/json" \
    -d '{"whisper": {"model": "small"}}')
assert_status "PUT /api/settings" 200 "$STATUS"
BODY=$(curl -s "$BASE/api/settings")
assert_contains "updated whisper model" "$BODY" '"model": "small"'
assert_contains "other settings preserved" "$BODY" '"context_model": "qwen3:8b"'

# Restore
curl -s -X PUT "$BASE/api/settings" -H "Content-Type: application/json" \
    -d '{"whisper": {"model": "medium"}}' > /dev/null

# ─── 3. Projects CRUD ───
echo ""
echo "[3] Projects — 初期状態"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/projects")
assert_status "GET /api/projects (empty)" 200 "$STATUS"
BODY=$(curl -s "$BASE/api/projects")
assert_contains "empty project list" "$BODY" '[]'

echo ""
echo "[4] Projects — 作成"
BODY=$(curl -s -w "\n%{http_code}" -X POST "$BASE/api/projects" \
    -H "Content-Type: application/json" \
    -d '{
        "name": "えがいて",
        "repo_path": "/tmp/seam-test-repo",
        "output_dir": "/tmp/seam-test-output1",
        "doc_dirs": ["/tmp/seam-test-docs"],
        "members": [{"name": "とちざわ", "role": "リード"}],
        "glossary": ["Supabase: BaaS"]
    }')
STATUS=$(echo "$BODY" | tail -1)
BODY=$(echo "$BODY" | head -n -1)
assert_status "POST /api/projects" 201 "$STATUS"
assert_contains "project name" "$BODY" '"name":"えがいて"'
assert_contains "repo_path" "$BODY" '"repo_path":"/tmp/seam-test-repo"'
assert_contains "members" "$BODY" '"とちざわ"'
assert_contains "glossary" "$BODY" '"Supabase: BaaS"'

# output_dir auto-creation
if [ -d "/tmp/seam-test-output1" ]; then
    echo "  PASS: output_dir auto-created"
    PASS=$((PASS + 1))
else
    echo "  FAIL: output_dir not auto-created"
    FAIL=$((FAIL + 1))
fi

PROJECT_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "  (created project_id: $PROJECT_ID)"

# Create second project
BODY2=$(curl -s -X POST "$BASE/api/projects" \
    -H "Content-Type: application/json" \
    -d '{"name": "みらもる", "output_dir": "/tmp/seam-test-output2"}')
PROJECT_ID2=$(echo "$BODY2" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo ""
echo "[5] Projects — 一覧 (2件)"
BODY=$(curl -s "$BASE/api/projects")
COUNT=$(echo "$BODY" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
if [ "$COUNT" = "2" ]; then
    echo "  PASS: 2 projects in list"
    PASS=$((PASS + 1))
else
    echo "  FAIL: expected 2 projects, got $COUNT"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "[6] Projects — 個別取得"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/projects/$PROJECT_ID")
assert_status "GET /api/projects/{id}" 200 "$STATUS"
BODY=$(curl -s "$BASE/api/projects/$PROJECT_ID")
assert_contains "project get by id" "$BODY" '"name":"えがいて"'

echo ""
echo "[7] Projects — 存在しないID"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/projects/nonexistent")
assert_status "GET /api/projects/nonexistent" 404 "$STATUS"

echo ""
echo "[8] Projects — 更新"
BODY=$(curl -s -w "\n%{http_code}" -X PUT "$BASE/api/projects/$PROJECT_ID" \
    -H "Content-Type: application/json" \
    -d '{"name": "えがいて v2", "glossary": ["Supabase: BaaS", "OG画像: プレビュー"]}')
STATUS=$(echo "$BODY" | tail -1)
BODY=$(echo "$BODY" | head -n -1)
assert_status "PUT /api/projects/{id}" 200 "$STATUS"
assert_contains "updated name" "$BODY" '"name":"えがいて v2"'
assert_contains "updated glossary" "$BODY" '"OG画像: プレビュー"'
# repo_path should be preserved
assert_contains "preserved repo_path" "$BODY" '"repo_path":"/tmp/seam-test-repo"'

echo ""
echo "[9] Projects — 存在しないIDの更新"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/projects/nonexistent" \
    -H "Content-Type: application/json" \
    -d '{"name": "test"}')
assert_status "PUT /api/projects/nonexistent" 404 "$STATUS"

# ─── 4. Minutes CRUD ───
echo ""
echo "[10] Minutes — 空の一覧"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/minutes")
assert_status "GET /api/minutes (empty)" 200 "$STATUS"
BODY=$(curl -s "$BASE/api/minutes")
assert_contains "empty minutes list" "$BODY" '[]'

echo ""
echo "[11] Minutes — プロジェクトフィルター"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/minutes?project=$PROJECT_ID")
assert_status "GET /api/minutes?project=..." 200 "$STATUS"

echo ""
echo "[12] Minutes — 存在しないID"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/minutes/nonexistent")
assert_status "GET /api/minutes/nonexistent" 404 "$STATUS"

echo ""
echo "[13] Minutes — 存在しないIDの削除"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/minutes/nonexistent")
assert_status "DELETE /api/minutes/nonexistent" 404 "$STATUS"

echo ""
echo "[14] Minutes — transcript取得 (存在しない)"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/minutes/nonexistent/transcript")
assert_status "GET /api/minutes/nonexistent/transcript" 404 "$STATUS"

echo ""
echo "[15] Minutes — speakers更新 (存在しない)"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/api/minutes/nonexistent/speakers" \
    -H "Content-Type: application/json" \
    -d '{"speaker_0": "テスト"}')
assert_status "PUT /api/minutes/nonexistent/speakers" 404 "$STATUS"

echo ""
echo "[16] Minutes — search (空)"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/minutes/search?q=テスト")
assert_status "GET /api/minutes/search" 200 "$STATUS"

# ─── 5. Projects — 削除 ───
echo ""
echo "[17] Projects — 削除 (output残す)"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/projects/$PROJECT_ID2")
assert_status "DELETE /api/projects/{id}" 200 "$STATUS"
if [ -d "/tmp/seam-test-output2" ]; then
    echo "  PASS: output_dir preserved after delete (default)"
    PASS=$((PASS + 1))
else
    echo "  FAIL: output_dir should be preserved"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "[18] Projects — 削除 (output削除)"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/projects/$PROJECT_ID?delete_output=true")
assert_status "DELETE /api/projects/{id}?delete_output=true" 200 "$STATUS"
if [ ! -d "/tmp/seam-test-output1" ]; then
    echo "  PASS: output_dir deleted with delete_output=true"
    PASS=$((PASS + 1))
else
    echo "  FAIL: output_dir should be deleted"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "[19] Projects — 削除後の一覧"
BODY=$(curl -s "$BASE/api/projects")
assert_contains "empty after delete" "$BODY" '[]'

echo ""
echo "[20] Projects — 存在しないIDの削除"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$BASE/api/projects/nonexistent")
assert_status "DELETE /api/projects/nonexistent" 404 "$STATUS"

# ─── 6. Validation ───
echo ""
echo "[21] Validation — 不正なJSON"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/projects" \
    -H "Content-Type: application/json" \
    -d 'invalid json')
assert_status "POST invalid JSON" 422 "$STATUS"

echo ""
echo "[22] Validation — 必須フィールド欠落"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/projects" \
    -H "Content-Type: application/json" \
    -d '{"name": "test"}')
assert_status "POST missing output_dir" 422 "$STATUS"

echo ""
echo "[23] Validation — search missing q"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/minutes/search")
assert_status "GET /api/minutes/search without q" 422 "$STATUS"

# ─── 7. WebSocket ───
echo ""
echo "[24] WebSocket — 接続テスト"
# Just verify the endpoint exists (timeout after 2 sec)
timeout 2 curl -s -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
    -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGVzdA==" \
    "$BASE/ws" > /dev/null 2>&1
WS_EXIT=$?
if [ $WS_EXIT -eq 124 ] || [ $WS_EXIT -eq 0 ]; then
    echo "  PASS: WebSocket endpoint responds"
    PASS=$((PASS + 1))
else
    echo "  FAIL: WebSocket endpoint failed (exit $WS_EXIT)"
    FAIL=$((FAIL + 1))
fi

# ─── Summary ───
echo ""
echo "========================================="
echo "  結果: $PASS passed, $FAIL failed"
echo "========================================="

# Cleanup
rm -rf /tmp/seam-test-output2

exit $FAIL
