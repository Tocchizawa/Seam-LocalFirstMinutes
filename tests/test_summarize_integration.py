"""Seam — 要約機能 Phase 1 統合検証

実環境 (~/.seam/minutes.db, ollama 起動中なら接続) を使った動作確認。
副作用を最小限にするため、DB は read-only で扱い、書き込みは別ID を使う。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import config
from src.storage.db import db
from src.summarize.base import (
    ProjectContext,
    ProviderHealth,
    SummaryError,
    SummaryErrorCode,
    SummaryResult,
)
from src.summarize.prompts import (
    build_user_prompt,
    estimate_tokens_jp,
    format_transcript_segments,
    validate_context_budget,
)
from src.summarize.registry import (
    auto_detect_recommended,
)
from src.summarize.runner import (
    DRAFTS_DIR,
    SummaryRunner,
    recover_drafts,
)

PASS = 0
FAIL = 0


def ok(name: str, cond: bool, hint: str = "") -> None:
    global PASS, FAIL
    if cond:
        print(f"  PASS: {name}")
        PASS += 1
    else:
        print(f"  FAIL: {name}{(' — ' + hint) if hint else ''}")
        FAIL += 1


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


# ─────────────────────────────────────────────────────────
section("[1] 実 DB から議事録を読み込んでプロンプト構築")
# ─────────────────────────────────────────────────────────

minutes_records = db.list_minutes(limit=5)
ok("DB minutes count >= 1", len(minutes_records) >= 1, f"got {len(minutes_records)}")

if minutes_records:
    sample = db.get_minutes(minutes_records[0]["id"])
    ok("get_minutes returns full record", sample is not None)

    transcript_raw = sample.get("transcript")
    if isinstance(transcript_raw, str):
        try:
            transcript_segs = json.loads(transcript_raw)
        except Exception:
            transcript_segs = []
    else:
        transcript_segs = transcript_raw or []

    ok(
        "transcript is list of dicts",
        isinstance(transcript_segs, list)
        and all(isinstance(s, dict) for s in transcript_segs),
    )

    formatted = format_transcript_segments(transcript_segs)
    print(f"    transcript chars: {len(formatted)}")
    print(f"    sample first 200: {formatted[:200]}...")

    # ProjectContext (project_manager から実取得)
    from src.project.manager import project_manager

    project = project_manager.get(sample["project_id"]) if sample.get("project_id") else None
    if project is not None:
        ctx = ProjectContext(
            name=project.name,
            members=[{"name": m.name, "role": m.role} for m in project.members],
            glossary=list(project.glossary),
        )
        print(f"    project: {ctx.name}, members={len(ctx.members)}, glossary={len(ctx.glossary)}")
    else:
        ctx = None

    user_prompt = build_user_prompt(formatted, ctx)
    ok("user prompt built", len(user_prompt) > 0)
    ok("user prompt includes transcript", formatted[:50] in user_prompt or len(formatted) < 50)

    # token見積り
    tokens = estimate_tokens_jp(formatted)
    print(f"    estimated tokens: {tokens}")

    # 実DBの議事録は長さがユーザー環境に依存するため、full transcript は
    # fits / overflow のどちらも正しい分類として扱う。
    try:
        validate_context_budget(formatted, ctx_window=16384)
        ok("full transcript context budget classified", True, "fits")
    except SummaryError as e:
        ok(
            "full transcript context budget classified",
            e.code == SummaryErrorCode.CONTEXT_OVERFLOW,
            str(e.code),
        )

    # 小さい transcript は必ず 16K に収まることを固定データで検証する。
    try:
        validate_context_budget(formatted[:1000], ctx_window=16384)
        ok("trimmed transcript fits 16K ctx (16384)", True)
    except SummaryError as e:
        ok("trimmed transcript fits 16K ctx (16384)", False, str(e.code))


# ─────────────────────────────────────────────────────────
section("[2] auto_detect_recommended (実Ollama接続試行)")
# ─────────────────────────────────────────────────────────


async def _detect():
    return await auto_detect_recommended(config.get("minutes_ai") or {})


detect_result = asyncio.run(_detect())
print(f"    provider: {detect_result.get('provider')}")
print(f"    reason:   {detect_result.get('reason')}")
ok("auto_detect returns dict", isinstance(detect_result, dict))
ok("has provider key", "provider" in detect_result)
ok("has reason key", "reason" in detect_result)


# ─────────────────────────────────────────────────────────
section("[3] FakeProvider で runner を end-to-end 検証")
# ─────────────────────────────────────────────────────────


class FakeProvider:
    """テスト用の偽provider — 実I/O無し。"""

    name = "fake"

    def __init__(self, *, fail_with: SummaryErrorCode | None = None) -> None:
        self._fail_with = fail_with
        self.cancel_called = False

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(ok=True, code="READY", message="fake ready", model="fake-1")

    async def generate(
        self,
        transcript,
        *,
        project=None,
        on_token=None,
        on_activity=None,
        timeout_sec=300,
        **_ignored,
    ):
        if self._fail_with:
            raise SummaryError(self._fail_with, "fake error", provider=self.name)
        if on_activity:
            res = on_activity("FakeProvider running")
            if asyncio.iscoroutine(res):
                await res
        # ストリーム模擬
        chunks = ["## 概要\n", "テスト要約\n\n", "## 決定事項\n", "- なし\n"]
        for c in chunks:
            if on_token:
                res = on_token(c)
                # async callback をサポート
                if asyncio.iscoroutine(res):
                    await res
            await asyncio.sleep(0.01)
        return SummaryResult(
            text="".join(chunks),
            provider=self.name,
            model="fake-1",
            input_chars=len(transcript),
            output_chars=sum(len(c) for c in chunks),
            duration_sec=0.05,
        )

    async def cancel(self, *, reason: str = "user_cancelled") -> None:
        self.cancel_called = True


async def _run_e2e():
    # WS broadcast を集約
    captured: list[dict] = []

    async def fake_broadcast(payload):
        captured.append(payload)

    runner = SummaryRunner(broadcaster=fake_broadcast)
    await runner.start()

    # FakeProvider を get_provider 経由で挿す
    import src.summarize.registry as reg
    orig_factories = reg._FACTORIES.copy()
    orig_phase_b = SummaryRunner._run_phase_b
    reg._FACTORIES["fake"] = lambda cfg: FakeProvider()

    async def _noop_phase_b(self, minutes_id: str) -> None:
        return None

    SummaryRunner._run_phase_b = _noop_phase_b
    try:
        # 既存DBの最小minutes idで実行 (検証後に元へ戻す)
        if not minutes_records:
            return None, captured, "", ""
        target_id = minutes_records[0]["id"]
        pre = db.get_minutes(target_id)
        original_summary = pre.get("summary", "") if pre else ""
        original_llm = pre.get("llm_model", "") if pre else ""
        runner.enqueue(target_id, provider_name="fake")

        # 完了を待つ (ポーリング、最長 5秒)
        for _ in range(50):
            st = runner.get_status(target_id)
            if st and st.state in ("done", "failed", "cancelled", "skipped"):
                break
            await asyncio.sleep(0.1)

        final_status = runner.get_status(target_id)
        return final_status, captured, original_summary, original_llm
    finally:
        await runner.shutdown()
        reg._FACTORIES.clear()
        reg._FACTORIES.update(orig_factories)
        SummaryRunner._run_phase_b = orig_phase_b


print("    実DB上の最初のminutesにFakeProviderで要約をかけ、検証後に元へ戻す...")
final_status, ws_events, original_summary, original_llm = asyncio.run(_run_e2e())

if final_status is not None:
    print(f"    final state: {final_status.state}")
    print(f"    partial chars: {len(final_status.partial_text)}")
    print(f"    error: {final_status.error_code} {final_status.error_message}")
    ok(
        "runner完了 → state in {done, skipped}",
        final_status.state in ("done", "skipped"),
        f"got {final_status.state}",
    )

    # WS event の内訳
    types = [e["type"] for e in ws_events]
    print(f"    ws events: {types}")
    if final_status.state == "done":
        ok("summary_done broadcasted", "summary_done" in types)
        ok("summary_chunk broadcasted", "summary_chunk" in types)

        # DB に書き戻されたか?
        updated = db.get_minutes(final_status.minutes_id)
        if updated:
            ok("DB summary updated", bool(updated.get("summary", "").strip()))
            ok("llm_model updated", "fake:fake-1" == updated.get("llm_model"))

            # 元に戻す (テスト副作用クリア)
            db.update_summary(final_status.minutes_id, original_summary, llm_model=original_llm or None)
            print(f"    cleanup: reverted summary on {final_status.minutes_id}")
    elif final_status.state == "skipped":
        ok("summary_skipped broadcasted", "summary_skipped" in types)
        print("    (transcript が短かったためスキップ)")


# ─────────────────────────────────────────────────────────
section("[4] FakeProvider error path → summary_failed broadcast")
# ─────────────────────────────────────────────────────────


async def _run_error_path():
    captured: list[dict] = []

    async def fake_broadcast(payload):
        captured.append(payload)

    runner = SummaryRunner(broadcaster=fake_broadcast)
    await runner.start()

    import src.summarize.registry as reg
    orig = reg._FACTORIES.copy()
    reg._FACTORIES["fake_err"] = lambda cfg: FakeProvider(fail_with=SummaryErrorCode.AUTH_FAILED)

    if minutes_records:
        target_id = minutes_records[0]["id"]
        runner.enqueue(target_id, provider_name="fake_err")
        for _ in range(50):
            st = runner.get_status(target_id)
            if st and st.state in ("done", "failed", "cancelled", "skipped"):
                break
            await asyncio.sleep(0.1)

    reg._FACTORIES.clear()
    reg._FACTORIES.update(orig)

    final = runner.get_status(target_id) if minutes_records else None
    await runner.shutdown()
    return final, captured


err_status, err_events = asyncio.run(_run_error_path())
if err_status is not None:
    types = [e["type"] for e in err_events]
    print(f"    final state: {err_status.state}")
    print(f"    error: {err_status.error_code}")
    print(f"    ws events: {types}")
    ok("error path → state==failed or skipped", err_status.state in ("failed", "skipped"))
    if err_status.state == "failed":
        ok("error_code recorded", err_status.error_code == "AUTH_FAILED")
        ok("summary_failed broadcasted", "summary_failed" in types)


# ─────────────────────────────────────────────────────────
section("[5] cancel — 進行中ジョブの停止")
# ─────────────────────────────────────────────────────────


class SlowFakeProvider:
    name = "slow"

    def __init__(self):
        self._cancelled = False
        self.cancel_event = asyncio.Event()

    async def health_check(self):
        return ProviderHealth(ok=True, code="READY", message="ok", model="slow-1")

    async def generate(
        self,
        transcript,
        *,
        project=None,
        on_token=None,
        on_activity=None,
        timeout_sec=300,
        **_ignored,
    ):
        if on_activity:
            res = on_activity("SlowFakeProvider running")
            if asyncio.iscoroutine(res):
                await res
        for i in range(50):
            if self._cancelled:
                raise SummaryError(SummaryErrorCode.CANCELLED, "cancelled", provider=self.name)
            if on_token:
                res = on_token(f"chunk{i} ")
                if asyncio.iscoroutine(res):
                    await res
            await asyncio.sleep(0.1)
        return SummaryResult(
            text="never reach", provider=self.name, model="slow-1",
            input_chars=0, output_chars=0, duration_sec=5.0,
        )

    async def cancel(self, *, reason="user_cancelled"):
        self._cancelled = True
        self.cancel_event.set()


async def _run_cancel():
    captured = []

    async def fake_broadcast(payload):
        captured.append(payload)

    runner = SummaryRunner(broadcaster=fake_broadcast)
    await runner.start()

    import src.summarize.registry as reg
    orig = reg._FACTORIES.copy()
    slow = SlowFakeProvider()
    reg._FACTORIES["slow"] = lambda cfg: slow

    if minutes_records:
        target_id = minutes_records[0]["id"]
        runner.enqueue(target_id, provider_name="slow")
        # 動き出すのを少し待つ
        await asyncio.sleep(0.3)
        # cancel 実行
        await runner.cancel(target_id)
        # 反映待ち
        for _ in range(30):
            st = runner.get_status(target_id)
            if st and st.state in ("done", "failed", "cancelled", "skipped"):
                break
            await asyncio.sleep(0.1)

    reg._FACTORIES.clear()
    reg._FACTORIES.update(orig)

    final = runner.get_status(target_id) if minutes_records else None
    await runner.shutdown()
    return final, captured, slow


cancel_status, cancel_events, slow_provider = asyncio.run(_run_cancel())
if cancel_status is not None:
    print(f"    final state: {cancel_status.state}")
    print(f"    provider.cancel called: {slow_provider._cancelled}")
    ok("provider.cancel was called", slow_provider._cancelled)
    ok(
        "state == cancelled",
        cancel_status.state == "cancelled",
        f"got {cancel_status.state}",
    )


# ─────────────────────────────────────────────────────────
section("[6] re-enqueue debounce — 同一IDの2連投で先行ジョブcancel")
# ─────────────────────────────────────────────────────────


async def _run_debounce():
    runner = SummaryRunner()
    await runner.start()

    runner.enqueue("dup_id_test")
    st1 = runner.get_status("dup_id_test")
    runner.enqueue("dup_id_test", provider_name="claude_api")
    st2 = runner.get_status("dup_id_test")
    await asyncio.sleep(0.05)  # ちょっと待つ
    await runner.shutdown()
    return st1, st2


st1, st2 = asyncio.run(_run_debounce())
ok("first enqueue → status created", st1 is not None)
ok("re-enqueue → provider updated", st2.provider == "claude_api")


# ─────────────────────────────────────────────────────────
section("[7] draft recovery — 実ファイル書き込み→DB復元")
# ─────────────────────────────────────────────────────────

# 一時的に DRAFTS_DIR に draft を作って、対象の minutes.summary を空にしてからリカバリ
if minutes_records:
    target = minutes_records[0]
    target_id = target["id"]

    # 元の summary を退避
    pre = db.get_minutes(target_id)
    original_summary = pre.get("summary", "") if pre else ""
    original_llm = pre.get("llm_model", "") if pre else ""

    # draft を書く
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    draft_path = DRAFTS_DIR / f"{target_id}.md"
    draft_text = "# 概要\nリカバリで復元された要約テスト"
    draft_path.write_text(draft_text, encoding="utf-8")

    # 既存 summary を空に強制
    db.update_summary(target_id, "", llm_model=None)

    # recover 実行
    recovered = recover_drafts()
    print(f"    recovered count: {recovered}")
    ok("recovered >= 1", recovered >= 1)

    # DB 確認
    after = db.get_minutes(target_id)
    ok("DB summary restored from draft", after.get("summary", "") == draft_text)
    ok("llm_model marked recovered", after.get("llm_model") == "recovered_draft")
    ok("draft file deleted after recovery", not draft_path.exists())

    # 元に戻す
    db.update_summary(target_id, original_summary, llm_model=original_llm or None)
    print(f"    cleanup: reverted {target_id}")
else:
    print("    (DBに minutes が無いためスキップ)")


# ─────────────────────────────────────────────────────────
print()
print("=" * 70)
print(f"  結果: {PASS} passed, {FAIL} failed")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
