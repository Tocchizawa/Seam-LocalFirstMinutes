"""Seam — 要約機能 Phase 1 (基盤) ユニットテスト

prompts / registry / runner の純粋ロジック部分を検証する。
provider実装やネットワークIOは触らない (Phase 2 以降の providerテストで対応)。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# プロジェクトルートを sys.path へ追加 (uv run python tests/... 直叩き対応)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# テストでは APP_DIR を一時dir に差し替えたいので、import 前に環境を設定
_TEST_APP_DIR = Path(tempfile.mkdtemp(prefix="seam_test_"))
os.environ["HOME"] = str(_TEST_APP_DIR.parent)

# config.py は import 時に APP_DIR を確定させる (~/.seam) ため、
# 後から差し替えるには monkeypatch が必要。テストの構造上、APP_DIR ベースの
# モジュールは import 時に副作用 (ディレクトリ作成等) を起こすため、
# 個別テストごとに APP_DIR を一時 dir にすり替える。

PASS = 0
FAIL = 0
TESTS: list[str] = []


def assert_eq(name: str, expected, actual) -> None:
    global PASS, FAIL
    if expected == actual:
        print(f"  PASS: {name}")
        PASS += 1
    else:
        print(f"  FAIL: {name}\n    expected: {expected!r}\n    actual:   {actual!r}")
        FAIL += 1
    TESTS.append(name)


def assert_true(name: str, cond: bool, hint: str = "") -> None:
    global PASS, FAIL
    if cond:
        print(f"  PASS: {name}")
        PASS += 1
    else:
        print(f"  FAIL: {name}{(' — ' + hint) if hint else ''}")
        FAIL += 1
    TESTS.append(name)


def assert_raises(name: str, exc_type, func, *args, **kwargs) -> None:
    global PASS, FAIL
    try:
        func(*args, **kwargs)
    except exc_type:
        print(f"  PASS: {name}")
        PASS += 1
        return
    except Exception as e:
        print(f"  FAIL: {name} — wrong exception {type(e).__name__}: {e}")
        FAIL += 1
        return
    print(f"  FAIL: {name} — no exception raised")
    FAIL += 1


# ─────────────────────────────────────────────────────────
# prompts.py のテスト
# ─────────────────────────────────────────────────────────

print("=" * 60)
print("[1] prompts.estimate_tokens_jp")
print("=" * 60)

from src.summarize.prompts import (
    SYSTEM_PROMPT,
    build_messages,
    build_user_prompt,
    estimate_tokens_jp,
    format_transcript_segments,
    is_too_short_for_summary,
    validate_context_budget,
)
from src.summarize.base import (
    ProjectContext,
    SummaryError,
    SummaryErrorCode,
)

assert_eq("empty string → 0", 0, estimate_tokens_jp(""))
# 9文字 → 9/1.3 + 1 ≒ 7-8
n9 = estimate_tokens_jp("こんにちは世界です")
assert_true("9文字 → 6-9 tokens", 6 <= n9 <= 9, f"got {n9}")
# 1万文字 ≒ 7700 tokens
n10k = estimate_tokens_jp("あ" * 10000)
assert_true("10000文字 ≒ 7700 tokens", 7000 <= n10k <= 8500, f"got {n10k}")


print()
print("=" * 60)
print("[2] prompts.validate_context_budget")
print("=" * 60)

# 短い transcript (1000文字) は 16K ctx で OK (DEFAULT_MAX_OUTPUT_TOKENS=8192のため)
try:
    needed = validate_context_budget("あ" * 1000, ctx_window=16384)
    assert_true("short transcript fits in 16K ctx", True)
    print(f"    needed={needed}")
except SummaryError as e:
    assert_true("short transcript fits in 16K ctx", False, str(e))

# 長すぎる transcript は overflow
def _overflow():
    validate_context_budget("あ" * 50000, ctx_window=8192)


assert_raises("overflow → SummaryError", SummaryError, _overflow)

# overflow エラーのコードチェック
try:
    validate_context_budget("あ" * 50000, ctx_window=8192)
except SummaryError as e:
    assert_eq("overflow code", SummaryErrorCode.CONTEXT_OVERFLOW, e.code)


print()
print("=" * 60)
print("[3] prompts.build_user_prompt / build_messages")
print("=" * 60)

# project未指定
prompt = build_user_prompt("[00:00] hello world")
assert_true("includes transcript", "hello world" in prompt)
assert_true("includes project header", "プロジェクト" in prompt)
assert_true("members 未指定 → '指定なし'", "(指定なし)" in prompt)

# project context 注入
ctx = ProjectContext(
    name="えがいて",
    members=[
        {"name": "田中", "role": "リード"},
        {"name": "佐藤", "role": ""},
    ],
    glossary=["Supabase: BaaS", "OG: SNSプレビュー"],
)
prompt2 = build_user_prompt("[00:00] テスト", ctx)
assert_true("project name 反映", "えがいて" in prompt2)
assert_true("member with role 反映", "田中 (リード)" in prompt2)
assert_true("member without role 反映", "- 佐藤" in prompt2 and "佐藤 ()" not in prompt2)
assert_true("glossary 反映", "Supabase: BaaS" in prompt2)

# build_messages
sys_p, user_p = build_messages("[00:00] hello", ctx)
assert_true("system prompt is template", sys_p == SYSTEM_PROMPT)
assert_true("user prompt is built", "hello" in user_p)


print()
print("=" * 60)
print("[4] prompts.format_transcript_segments")
print("=" * 60)

segs = [
    {"start": 0.0, "end": 5.0, "text": "おはようございます", "speaker_label": "田中"},
    {"start": 65.5, "end": 70.0, "text": "今日のアジェンダは...", "speaker_label": "佐藤"},
    {"start": 130.0, "end": 132.0, "text": "了解です"},  # speaker_label 無し
    {"start": 0, "end": 0, "text": ""},  # 空 → skip
]
formatted = format_transcript_segments(segs)
assert_true("[00:00] 形式 with speaker", "[00:00] (田中) おはよう" in formatted)
assert_true("[01:05] 形式", "[01:05] (佐藤) 今日のアジェンダ" in formatted)
assert_true("speaker 無しなら timestamp + text", "[02:10] 了解です" in formatted)
assert_true("空 segment skip", formatted.count("\n") == 2)


print()
print("=" * 60)
print("[5] prompts.is_too_short_for_summary")
print("=" * 60)

assert_eq("空 → True", True, is_too_short_for_summary(""))
assert_eq("極短 → True", True, is_too_short_for_summary("hi"))
assert_eq("50文字未満 → True", True, is_too_short_for_summary("a" * 49))
assert_eq("50文字以上 → False", False, is_too_short_for_summary("a" * 60))


print()
print("=" * 60)
print("[6] base.SummaryError")
print("=" * 60)

err = SummaryError(SummaryErrorCode.AUTH_FAILED, "bad key", provider="claude_api")
assert_eq("error code", SummaryErrorCode.AUTH_FAILED, err.code)
assert_eq("error message", "bad key", err.message)
assert_eq("error provider", "claude_api", err.provider)
d = err.to_dict()
assert_eq("to_dict code", "AUTH_FAILED", d["code"])
assert_eq("to_dict message", "bad key", d["message"])
assert_eq("to_dict provider", "claude_api", d["provider"])


print()
print("=" * 60)
print("[7] registry.is_cloud_provider / is_known_provider")
print("=" * 60)

from src.summarize.registry import (
    is_cloud_provider,
    is_known_provider,
)

assert_eq("ollama not cloud", False, is_cloud_provider("ollama"))
assert_eq("claude_api cloud", True, is_cloud_provider("claude_api"))
assert_eq("openai cloud", True, is_cloud_provider("openai"))
assert_eq("gemini cloud", True, is_cloud_provider("gemini"))
assert_eq("unknown not cloud", False, is_cloud_provider("foobar"))
assert_eq("ollama known", True, is_known_provider("ollama"))
assert_eq("unknown not known", False, is_known_provider("foobar"))


print()
print("=" * 60)
print("[8] registry.get_provider — unknown provider")
print("=" * 60)

from src.summarize.registry import get_provider

try:
    get_provider("nonexistent", {})
    assert_true("get_provider raises for unknown", False, "no exception")
except SummaryError as e:
    assert_eq(
        "unknown provider error code",
        SummaryErrorCode.NOT_CONFIGURED,
        e.code,
    )


print()
print("=" * 60)
print("[9] registry.auto_detect_recommended (Ollama unreachable)")
print("=" * 60)

# httpx でローカル疎通失敗想定 (架空ポート)
async def _detect():
    return await __import__("src.summarize.registry", fromlist=["auto_detect_recommended"]).auto_detect_recommended(
        {"ollama": {"base_url": "http://127.0.0.1:1"}}
    )


# auto_detect の優先順位:
#   ollama → cloud key → claude/codex CLI → ollama (no model) → None
# 全て無い状態を作るため、shutil.which と keyring を共にmock。
with patch.object(
    __import__("src.summarize.registry", fromlist=["_has_keyring_token"]),
    "_has_keyring_token",
    return_value=False,
):
    with patch("shutil.which", return_value=None):
        res = asyncio.run(_detect())
assert_eq("no provider when unreachable + no key + no CLI", None, res["provider"])
assert_true("reason mentions setup", "Ollama" in res["reason"] or "API" in res["reason"])


# ─────────────────────────────────────────────────────────
# config migration
# ─────────────────────────────────────────────────────────

print()
print("=" * 60)
print("[10] config: minutes_ai migration & defaults")
print("=" * 60)

# Config クラスを直接テスト (一時dir で)
from src.config import Config
import yaml

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    # 旧形式 yaml を書き込む
    legacy_yaml = {
        "schema_version": 1,
        "ollama": {
            "base_url": "http://localhost:11434",
            "context_model": "qwen3:8b",
            "minutes_model": "qwen3:14b",      # ← 移行されるはず
            "minutes_num_ctx": 4096,
            "minutes_num_thread": 2,
            "minutes_num_gpu": 0,
            "minutes_low_vram": True,
            "auto_start": True,
        },
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(legacy_yaml, allow_unicode=True), encoding="utf-8")

    # APP_DIR を tmp_path にすり替えた Config を生成 (monkey patching)
    import src.config as config_mod
    orig_app_dir = config_mod.APP_DIR
    config_mod.APP_DIR = tmp_path
    try:
        c = Config()
        c._path = cfg_path  # 念のため明示
        c.load()
        ai_ollama = c.get("minutes_ai", "ollama") or {}
        assert_eq("migrated model from minutes_model", "qwen3:14b", ai_ollama.get("model"))
        assert_eq("num_thread migrated", 2, ai_ollama.get("num_thread"))
        # num_ctx は最低2048で正規化される
        assert_true(
            "num_ctx migrated and clamped >= 2048",
            ai_ollama.get("num_ctx", 0) >= 2048,
        )
        # 旧キーが削除されているか
        old_ollama = c.get("ollama") or {}
        assert_true(
            "legacy minutes_model removed from ollama.*",
            "minutes_model" not in old_ollama,
        )
        assert_true(
            "legacy minutes_num_ctx removed from ollama.*",
            "minutes_num_ctx" not in old_ollama,
        )
        # context_model は残っている
        assert_eq(
            "context_model preserved", "qwen3:8b", old_ollama.get("context_model")
        )
        # auto_generate デフォルト
        assert_eq("auto_generate default True", True, c.get("minutes_ai", "auto_generate"))
        # consent デフォルト全て False
        consent = c.get("minutes_ai", "consent") or {}
        assert_eq("consent claude_api default", False, consent.get("claude_api"))
        assert_eq("consent openai default", False, consent.get("openai"))
        assert_eq("consent gemini default", False, consent.get("gemini"))
        assert_eq(
            "claude_code connect_timeout_sec default",
            12,
            c.get("minutes_ai", "claude_code", "connect_timeout_sec"),
        )
        assert_eq(
            "codex connect_timeout_sec default",
            30,
            c.get("minutes_ai", "codex", "connect_timeout_sec"),
        )
        c.update({
            "minutes_ai": {
                "codex": {
                    "model": "gpt-5.4",
                    "launcher_command": "source ~/.zshrc; my_codex",
                    "connect_timeout_sec": 5,
                }
            }
        })
        assert_eq(
            "codex model preserved",
            "gpt-5.4",
            c.get("minutes_ai", "codex", "model"),
        )
        assert_eq(
            "codex launcher_command preserved",
            "source ~/.zshrc; my_codex",
            c.get("minutes_ai", "codex", "launcher_command"),
        )
        assert_eq(
            "codex connect_timeout_sec preserved",
            5,
            c.get("minutes_ai", "codex", "connect_timeout_sec"),
        )
        c.update({"minutes_ai": {"codex": {"connect_timeout_sec": 12}}})
        assert_eq(
            "codex legacy default timeout migrated",
            30,
            c.get("minutes_ai", "codex", "connect_timeout_sec"),
        )
        assert_eq("app update check_on_startup default", True, c.get("app_update", "check_on_startup"))
        assert_eq(
            "app update auto_install_on_startup default",
            False,
            c.get("app_update", "auto_install_on_startup"),
        )
        c.update({"app_update": {"check_on_startup": False, "auto_install_on_startup": True}})
        assert_eq(
            "auto install forces startup check",
            True,
            c.get("app_update", "check_on_startup"),
        )
    finally:
        config_mod.APP_DIR = orig_app_dir


# ─────────────────────────────────────────────────────────
# runner enqueue + status (process_job は実providerが必要なので除外)
# ─────────────────────────────────────────────────────────

print()
print("=" * 60)
print("[11] runner enqueue / cancel / status")
print("=" * 60)

from src.summarize.runner import SummaryRunner

# enqueue 直後の status
runner = SummaryRunner()
runner.enqueue("test_id_1")
status = runner.get_status("test_id_1")
assert_true("enqueue creates status", status is not None)
assert_eq("initial state queued", "queued", status.state)
assert_eq("initial provider None", None, status.provider)

# 再enqueue → 既存cancel
runner.enqueue("test_id_1", provider_name="claude_api")
status2 = runner.get_status("test_id_1")
assert_eq("re-enqueue updates provider", "claude_api", status2.provider)


# ─────────────────────────────────────────────────────────
# recovery 模擬
# ─────────────────────────────────────────────────────────

print()
print("=" * 60)
print("[12] recover_drafts (空ディレクトリ)")
print("=" * 60)

# DRAFTS_DIR が存在しないとき → 0件
import src.summarize.runner as runner_mod
orig_drafts = runner_mod.DRAFTS_DIR
with tempfile.TemporaryDirectory() as tmp:
    runner_mod.DRAFTS_DIR = Path(tmp) / "nonexistent"
    try:
        n = runner_mod.recover_drafts()
        assert_eq("recover_drafts no dir → 0", 0, n)
    finally:
        runner_mod.DRAFTS_DIR = orig_drafts


# ─── Summary ───
print()
print("=" * 60)
print(f"  結果: {PASS} passed, {FAIL} failed")
print("=" * 60)

# クリーンアップ
shutil.rmtree(_TEST_APP_DIR, ignore_errors=True)

sys.exit(0 if FAIL == 0 else 1)
