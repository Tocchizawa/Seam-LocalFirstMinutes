"""Deterministic contract coverage for v2 semantic operations (no provider calls)."""

import json
import asyncio
import sqlite3
import uuid
from pathlib import Path

import jsonschema
import pytest

from src.live_notes.models import (
    DocumentEdit,
    DocumentEditsRequest,
    HumanOperation,
    LiveSettings,
    SemanticOperation,
    SemanticOperationPlan,
    semantic_operation_plan_schema,
)
from src.live_notes.repository import LiveNoteStore, NoteError


FIXTURE = Path(__file__).parent / "fixtures/live_notes_v2_scenario.json"
MODEL_RESPONSE_FIXTURE = Path(__file__).parent / "fixtures/v2/model-response-canonical-terms.json"
FORWARD_TOPIC_FIXTURE = Path(__file__).parent / "fixtures/v2/model-response-forward-topic-ref.json"


@pytest.fixture
def store(tmp_path):
    db = LiveNoteStore(tmp_path / "v2.sqlite")
    db.create("v2-meeting", "project", "codex", {"model": "synthetic"}, LiveSettings().model_dump())
    yield db
    db.close()


def add_batch(store, text):
    store.ingest("v2-meeting", [{"segment_id": uuid.uuid4().hex, "start": 0, "end": 1, "text": text}])
    batch = store.batch("v2-meeting")
    return batch, store.context("v2-meeting", batch)


def apply_v2(store, batch, context, *ops):
    return apply_v2_to(store, "v2-meeting", batch, context, *ops)


def apply_v2_to(store, sid, batch, context, *ops):
    raw = SemanticOperationPlan(
        schema_version=2,
        batch_id=batch["id"],
        base_revision=context["base_revision"],
        reason="synthetic contract test",
        operations=[SemanticOperation(op_id=f"{batch['id']}:{i}", source_segments=[batch["seq_to"]], **op) for i, op in enumerate(ops, 1)],
    ).model_dump()
    jsonschema.validate(raw, semantic_operation_plan_schema())
    return store.apply(sid, batch, context, raw)


def add_topic(store, title="NRI接続"):
    batch, context = add_batch(store, "テーマを確認する")
    result, _ = store.apply("v2-meeting", batch, context, {
        "schema_version": 1, "batch_id": batch["id"], "base_note_revision": context["base_note_revision"],
        "operations": [{"op_id": batch["id"]+":1", "action":"create_topic", "topic_id":"new:1", "block_id":None,
          "expected_block_revision":None, "title":title, "kind":None,"markdown":None,"source_segment_seqs":[batch["seq_to"]],"confidence":0.9}]
    })
    return result["topics"][0]["id"]


def test_v2_scenario_fixture_is_synthetic_and_has_unanswered_5_min_checkpoint():
    scenario = json.loads(FIXTURE.read_text())
    primary = scenario["primary_scenario"]
    checkpoint = primary["checkpoints"][0]
    assert checkpoint["through_seq"] == 4
    assert any("unanswered/open" in item for item in checkpoint["evaluation_focus"])
    assert scenario["comparison_variation"]["scenario_type"].startswith("option_comparison")


def test_scenario_fixture_replays_through_store_at_checkpoints_and_separate_meeting(store):
    scenario = json.loads(FIXTURE.read_text())
    primary = scenario["primary_scenario"]
    sid = "v2-meeting"
    topic_id = question_id = primary_block_id = term_block_id = None
    checkpoints = primary["checkpoints"]
    observed = []
    for segment in primary["segments"]:
        seq = segment["seq"]
        store.ingest(sid, [{"segment_id": f"fixture-{seq}", "start":segment["time_sec"],
            "end":segment["time_sec"]+1, "text":segment["text"]}])
        batch = store.batch(sid)
        context = store.context(sid, batch)
        ops = [{"type":"NO_CHANGE"}]
        if seq == 1:
            ops = [{"type":"CREATE_TOPIC", "topic_id":"new:1", "title":"NRI共通部品の受付接続"},
                {"type":"UPSERT_KNOWLEDGE", "topic_id":"new:1", "semantic_role":"fact",
                 "epistemic_state":"supported", "content_md":"NRI共通部品をmain.goで生成しgRPCサーバーへ渡す。"}]
        elif seq == 2:
            ops = [{"type":"UPSERT_KNOWLEDGE", "topic_id":topic_id, "question_id":"fixture-nri-open",
                "semantic_role":"open_question", "epistemic_state":"tentative", "resolution_state":"open",
                "content_md":"NRI共通部品は受付ハンドラーまで届き、そこで使われるか。"}]
        elif seq == 5:
            q = next(q for q in store.snapshot(sid)["questions"] if q["id"] == question_id)
            ops = [{"type":"RESOLVE_QUESTION", "question_id":question_id, "expected_revision":q["revision"],
                "semantic_role":"conclusion", "epistemic_state":"supported",
                "content_md":"NewNriPointには依存が渡されず、受付側では使われない。"}]
        elif seq == 6:
            ops = [{"type":"UPSERT_KNOWLEDGE", "topic_id":topic_id, "semantic_role":"explanation",
                "epistemic_state":"supported", "content_md":"生成と受付側で使われることは別である。"}]
        elif seq == 7:
            ops = [{"type":"UPSERT_KNOWLEDGE", "topic_id":topic_id, "semantic_role":"explanation",
                "epistemic_state":"supported", "content_md":"grpcserver.NewServerはサーバー本体を作り、NewNriPointは受付部品を作る。"}]
        elif seq == 9:
            ops = [{"type":"UPSERT_KNOWLEDGE", "topic_id":topic_id, "semantic_role":"option",
                "epistemic_state":"tentative", "content_md":"接続候補は依存を保持してNewNriPointへ渡すこと。業務処理の実装状況は別途確認する。"}]
        elif seq == 11:
            ops = [{"type":"CREATE_TOPIC", "topic_id":"new:1", "title":"設定ファイル"}]
        elif seq == 12:
            settings_topic = next(t["id"] for t in store.snapshot(sid)["topics"] if t["title"] == "設定ファイル")
            ops = [{"type":"UPSERT_KNOWLEDGE", "topic_id":settings_topic, "semantic_role":"fact",
                "epistemic_state":"supported", "content_md":"設定はホーム配下のconfig.yamlに保存される。"}]
        elif seq == 13:
            current = next(b for t in store.snapshot(sid)["topics"] for b in t["blocks"] if b["id"] == primary_block_id)
            ops = [{"type":"UPSERT_KNOWLEDGE", "topic_id":topic_id, "block_id":primary_block_id,
                "expected_revision":current["revision"], "semantic_role":"fact", "epistemic_state":"supported",
                "content_md":"NewGRPCServerからNewNriPointに依存は渡らず、受付では使われない。"}]
        elif seq == 14:
            ops = [{"type":"UPSERT_KNOWLEDGE", "topic_id":topic_id, "semantic_role":"fact",
                "epistemic_state":"supported", "content_md":"画面表記はNriDeps。", "aliases":["NriDeps"]}]
        elif seq == 15:
            ops = [{"type":"UPSERT_KNOWLEDGE", "topic_id":topic_id, "semantic_role":"option",
                "epistemic_state":"tentative", "content_md":"接続方針は候補であり、担当者と期限は決まっていない。"}]
        result, _ = apply_v2(store, batch, context, *ops)
        if seq == 1:
            topic_id = result["topics"][0]["id"]
        if seq == 2:
            question_id = result["questions"][0]["id"]
        if seq == 1:
            primary_block_id = result["topics"][0]["blocks"][0]["id"]
        if seq == 14:
            term_block_id = next(b["id"] for t in result["topics"] for b in t["blocks"] if "NriDeps" in b["content_md"])
            term = next(b for t in result["topics"] for b in t["blocks"] if b["id"] == term_block_id)
            result = store.human(sid, "edit_block", HumanOperation(request_id=primary["manual_correction_event"]["request_id"],
                expected_revision=term["revision"], content_md="画面表記はNriDependencies。"), term_block_id)
        if any(item["through_seq"] == seq and item["at_time_sec"] <= 1080 for item in checkpoints):
            snap = store.snapshot(sid)
            observed.append((seq, snap))
            if seq == 4:
                assert snap["questions"][0]["state"] == "open"
                assert len(snap["topics"]) == 1
            if seq == 10:
                assert next(q for q in snap["questions"] if q["id"] == question_id)["state"] == "answered"
                assert len(snap["topics"]) == 1
    store.recover()
    final_batch = store.finalize_batch(sid)
    assert final_batch and final_batch["kind"] == "finalize"
    final_context = store.context(sid, final_batch)
    final, _ = apply_v2_to(store, sid, final_batch, final_context, {"type":"NO_CHANGE"})
    observed.append((15, final))
    assert len(observed) == 4
    assert len(final["topics"]) == 2
    assert term_block_id and next(b for t in final["topics"] for b in t["blocks"] if b["id"] == term_block_id)["owner"] == "human"
    assert "NriDependencies" in final["document"]["markdown"]
    assert "**論点:**" not in final["document"]["markdown"]

    other = "v2-comparison"
    store.create(other, "project", "codex", {"model":"synthetic"}, LiveSettings().model_dump())
    comparison_topic = None
    for segment in scenario["comparison_variation"]["segments"]:
        store.ingest(other, [{"segment_id":f"comparison-{segment['seq']}", "start":segment["time_sec"],
            "end":segment["time_sec"]+1,"text":segment["text"]}])
        b = store.batch(other); c = store.context(other,b)
        if segment["seq"] == 1:
            operations = [{"type":"CREATE_TOPIC", "topic_id":"new:1", "title":"A案とB案の比較"},
                {"type":"UPSERT_KNOWLEDGE", "topic_id":"new:1", "semantic_role":"option", "epistemic_state":"supported",
                 "content_md":"A案は初期費用が低いが運用担当を多く必要とする。"}]
        elif segment["seq"] == 2:
            operations = [{"type":"UPSERT_KNOWLEDGE", "topic_id":comparison_topic, "semantic_role":"option", "epistemic_state":"supported",
                "content_md":"B案は導入費用が高い一方、毎月の運用コストは低い。"}]
        elif segment["seq"] == 3:
            operations = [{"type":"UPSERT_KNOWLEDGE", "topic_id":comparison_topic, "semantic_role":"option", "epistemic_state":"tentative",
                "content_md":"現時点ではB案がよさそうだが、法務レビュー後に決める。"}]
        elif segment["seq"] == 4:
            operations = [{"type":"UPSERT_KNOWLEDGE", "topic_id":comparison_topic, "question_id":"legal-review",
                "semantic_role":"open_question", "epistemic_state":"tentative", "resolution_state":"open",
                "content_md":"法務レビューが完了したか。"}]
        snap, _ = apply_v2_to(store, other, b, c, *operations)
        if segment["seq"] == 1:
            comparison_topic = snap["topics"][0]["id"]
    assert comparison_topic and store.snapshot(other)["topics"][0]["title"] == "A案とB案の比較"
    comparison = store.snapshot(other)
    assert comparison["questions"][0]["state"] == "open"
    assert all(b["epistemic_state"] != "confirmed" for b in comparison["topics"][0]["blocks"])


def test_v2_schema_requires_type_specific_payload_and_rejects_unused_fields():
    schema = semantic_operation_plan_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    valid = SemanticOperationPlan(schema_version=2, batch_id="b", base_revision=0, operations=[
        SemanticOperation(op_id="b:1", type="UPDATE_TAKEAWAY", content_md="結論", source_segments=[1])
    ]).model_dump()
    jsonschema.validate(valid, schema)
    invalid = json.loads(json.dumps(valid))
    invalid["operations"][0]["title"] = "不要"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)
    with pytest.raises(Exception):
        SemanticOperation(op_id="b:1", type="UPDATE_TAKEAWAY", source_segments=[1])
    update = SemanticOperationPlan(schema_version=2, batch_id="b", base_revision=0, operations=[
        SemanticOperation(op_id="b:1", type="UPSERT_KNOWLEDGE", topic_id="t", block_id="existing",
            expected_revision=3, semantic_role="fact", content_md="updated")
    ]).model_dump()
    jsonschema.validate(update, schema)
    update["operations"][0]["expected_revision"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(update, schema)


def test_captured_cli_response_uses_canonical_terms_on_resolve_and_takeaway():
    raw = json.loads(MODEL_RESPONSE_FIXTURE.read_text())
    plan = SemanticOperationPlan.model_validate(raw)
    jsonschema.validate(plan.model_dump(), semantic_operation_plan_schema())
    assert any(op.type == "RESOLVE_QUESTION" and op.canonical_terms for op in plan.operations)
    assert any(op.type == "UPDATE_TAKEAWAY" and op.canonical_terms for op in plan.operations)


def test_captured_plan_resolves_topic_created_later_in_operation_list(tmp_path):
    raw = json.loads(FORWARD_TOPIC_FIXTURE.read_text())
    sid = "synthetic-tech-meeting-001"
    store = LiveNoteStore(tmp_path / "forward-topic.sqlite")
    try:
        store.create(sid,"project","codex",{"model":"synthetic"},LiveSettings().model_dump())
        scenario = json.loads(FIXTURE.read_text())["primary_scenario"]
        store.ingest(sid,[{"segment_id":f"segment-{item['seq']}","start":item["time_sec"],
            "end":item["time_sec"]+10,"text":item["text"]} for item in scenario["segments"]])
        topic_id = "1ea2b216185b479eafa39afa2f117f81"
        block_id = "653d4bd59b824f94969350f8e3b2f9dc"
        stamp = "2026-01-01T00:00:00+00:00"
        store.conn.execute("INSERT INTO live_note_topics(id,session_id,title,title_owner,position,revision,created_at,updated_at) VALUES(?,?,?,'ai',0,0,?,?)",
            (topic_id,sid,"NRI共通部品の受付接続",stamp,stamp))
        store.conn.execute("INSERT INTO live_note_blocks(id,topic_id,kind,content_md,owner,position,revision,source_segments_json,created_at,updated_at,semantic_role,epistemic_state,aliases_json,canonical_terms_json,semantic_v2) VALUES(?,?, 'point',?,'ai',0,0,?,?,?,'option','tentative','[\"NriDeps\"]','[]',1)",
            (block_id,topic_id,"接続候補は未確認。","[9]",stamp,stamp))
        store.conn.execute("UPDATE live_note_sessions SET last_processed_segment_seq=10,note_revision=3,stopped=1,status='finalizing' WHERE session_id=?",(sid,))
        batch = store.batch(sid)
        assert batch["id"] == raw["batch_id"] and (batch["seq_from"],batch["seq_to"]) == (11,15)
        assert batch["kind"] == "finalize"
        context = store.context(sid,batch)
        plan = SemanticOperationPlan.model_validate(raw)
        applied, conflicts = store.apply(sid,batch,context,plan.model_dump())
        assert conflicts == []
        setting = next(t for t in applied["topics"] if t["title"] == "設定ファイルの保存場所")
        assert any(b["content_md"].startswith("設定はユーザー") for b in setting["blocks"])
        corrected = next(b for t in applied["topics"] for b in t["blocks"] if b["id"] == block_id)
        assert "NriDependencies" in corrected["content_md"] and corrected["aliases"] == ["NriDeps"]
        takeaway = next(n for n in applied["document"]["nodes"] if n.get("is_takeaway"))
        assert takeaway["markdown"] == applied["takeaway"] and "config.yaml" in takeaway["markdown"]
        assert applied["note_revision"] == 4 and applied["last_processed_segment_seq"] == 15
        assert applied["finalize_pass_done"] is True and applied["pending_segments"] == 0
        assert store.finalize_batch(sid) is None
    finally:
        store.close()


def test_unused_collection_fields_are_empty_in_model_schema():
    schema = semantic_operation_plan_schema()
    raw = SemanticOperationPlan(schema_version=2,batch_id="b",base_revision=0,operations=[
        SemanticOperation(op_id="b:1",type="NO_CHANGE")]).model_dump()
    jsonschema.validate(raw,schema)
    raw["operations"][0]["aliases"] = ["unexpected"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(raw,schema)


def test_prompt_requires_document_structure_and_pass_specific_review():
    from src.live_notes.prompts import build_prompt
    prompt = build_prompt({"batch_kind":"fast", "segments":[]})
    assert "short organization plan" in prompt
    assert "one block per independently useful idea" in prompt
    assert "visible open_question block and question record" in prompt
    assert "secondary topics such as configuration" in prompt
    assert "UPDATE_TAKEAWAY" in prompt
    assert "consolidation pass" in build_prompt({"batch_kind":"consolidate"})
    assert "final pass after capture stopped" in build_prompt({"batch_kind":"finalize"})


@pytest.mark.parametrize("payload", [
    {"type":"CREATE_TOPIC","title":"Topic"},
    {"type":"UPSERT_KNOWLEDGE","topic_id":"t","semantic_role":"fact","content_md":"fact"},
    {"type":"RESOLVE_QUESTION","question_id":"q","expected_revision":0,"content_md":"answer"},
    {"type":"REVISE_CLAIM","block_id":"b","expected_revision":0,"content_md":"revised"},
    {"type":"MERGE_BLOCKS","source_block_ids":["b2"],"target_block_id":"b1","expected_revisions":{"b1":0,"b2":0},"content_md":"merged"},
    {"type":"MOVE_BLOCK","block_id":"b","expected_revision":0,"target_topic_id":"t"},
    {"type":"RESTRUCTURE_TOPIC","topic_id":"t","expected_topic_revisions":{"t":0}},
    {"type":"UPDATE_TAKEAWAY","content_md":"takeaway"},
    {"type":"SUGGEST_EDIT","target_type":"block","target_id":"b","expected_revision":0,"content_md":"suggestion"},
    {"type":"NO_CHANGE"},
])
def test_each_semantic_operation_has_a_valid_typed_schema_variant(payload):
    raw=SemanticOperationPlan(schema_version=2,batch_id="b",base_revision=0,reason="test",operations=[
        SemanticOperation(op_id="b:1",source_segments=[1],**payload)
    ]).model_dump()
    jsonschema.validate(raw,semantic_operation_plan_schema())


def test_v1_migration_is_non_destructive_and_idempotent(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE minutes(id TEXT PRIMARY KEY, summary TEXT); INSERT INTO minutes VALUES('m','original');")
    conn.close()
    first = LiveNoteStore(path)
    first.close()
    second = LiveNoteStore(path)
    try:
        assert second.conn.execute("SELECT summary FROM minutes WHERE id='m'").fetchone()[0] == "original"
        assert second.conn.execute("SELECT count(*) FROM live_note_schema").fetchone()[0] == 1
        assert second.conn.execute("SELECT version FROM live_note_schema").fetchone()[0] == 3
        columns = {row[1] for row in second.conn.execute("PRAGMA table_info(live_note_blocks)")}
        assert {"semantic_role", "user_touched", "aliases_json", "superseded_by"} <= columns
    finally:
        second.close()


def test_populated_v1_notes_migrate_without_losing_ids_text_or_evidence(tmp_path):
    path = tmp_path / "populated-v1.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE live_note_schema(version INTEGER PRIMARY KEY);
      INSERT INTO live_note_schema VALUES(1);
      CREATE TABLE live_note_sessions(session_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,minutes_id TEXT UNIQUE,provider TEXT NOT NULL,status TEXT NOT NULL,note_revision INTEGER NOT NULL DEFAULT 0,last_processed_segment_seq INTEGER NOT NULL DEFAULT 0,active_topic_id TEXT,last_error TEXT,started_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,provider_config_json TEXT NOT NULL DEFAULT '{}',live_config_json TEXT NOT NULL DEFAULT '{}',meeting_meta_json TEXT NOT NULL DEFAULT '{}',paused INTEGER NOT NULL DEFAULT 0,stopped INTEGER NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL DEFAULT 0,flush_requested INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE live_note_topics(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,title TEXT NOT NULL,title_owner TEXT NOT NULL,position INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE TABLE live_note_blocks(id TEXT PRIMARY KEY,topic_id TEXT NOT NULL,kind TEXT NOT NULL,content_md TEXT NOT NULL,owner TEXT NOT NULL,position INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 0,source_segments_json TEXT NOT NULL DEFAULT '[]',deleted_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      INSERT INTO live_note_sessions(session_id,project_id,provider,status,started_at,updated_at,provider_config_json,live_config_json) VALUES('old-session','project','codex','completed','2026-01-01','2026-01-01','{}','{}');
      INSERT INTO live_note_topics(id,session_id,title,title_owner,position,revision,created_at,updated_at) VALUES('old-topic','old-session','既存の話題','human',0,7,'2026-01-01','2026-01-01');
      INSERT INTO live_note_blocks(id,topic_id,kind,content_md,owner,position,revision,source_segments_json,created_at,updated_at) VALUES('old-block','old-topic','decision','既存の合意','human',0,9,'[2,5]','2026-01-01','2026-01-01');
    """)
    conn.close()
    migrated = LiveNoteStore(path)
    try:
        snapshot = migrated.snapshot("old-session")
        assert snapshot["topics"][0]["id"] == "old-topic"
        block = snapshot["topics"][0]["blocks"][0]
        assert (block["id"], block["content_md"], block["revision"], block["source_segment_seqs"]) == ("old-block", "既存の合意", 9, [2, 5])
        assert block["semantic_v2"] == 0
        assert migrated.conn.execute("SELECT version FROM live_note_schema WHERE version=3").fetchone()[0] == 3
    finally:
        migrated.close()


def test_snapshot_exposes_stable_document_projection_without_fixed_labels(store):
    topic = add_topic(store)
    batch, context = add_batch(store, "受付の依存を確認する")
    result, _ = apply_v2(store,batch,context,{"type":"UPSERT_KNOWLEDGE","topic_id":topic,
        "semantic_role":"conclusion","epistemic_state":"supported","content_md":"依存はまだ受付に接続されていない。"})
    doc=result["document"]
    assert result["document_revision"] == result["note_revision"]
    assert [n["type"] for n in doc["nodes"]] == ["topic","block"]
    assert doc["nodes"][0]["id"] == topic and doc["nodes"][1]["topic_id"] == topic
    assert "**論点:**" not in doc["markdown"] and "接続されていない" in doc["markdown"]


def test_transcript_evidence_cannot_claim_external_source_verification(store):
    topic = add_topic(store)
    batch, context = add_batch(store, "受付の実装を確認する")
    with pytest.raises(NoteError) as error:
        apply_v2(store, batch, context, {"type":"UPSERT_KNOWLEDGE", "topic_id":topic,
            "semantic_role":"fact", "content_md":"受付は未接続。", "verified_against_source":True})
    assert error.value.code == "INVALID_EVIDENCE"
    assert store.snapshot("v2-meeting")["note_revision"] == context["base_note_revision"]


def test_takeaway_is_present_in_document_and_export_markdown(store):
    topic = add_topic(store)
    batch, context = add_batch(store, "話した内容をまとめる")
    snapshot, _ = apply_v2(store, batch, context,
        {"type":"UPSERT_KNOWLEDGE", "topic_id":topic, "semantic_role":"fact", "content_md":"受付には依存が届かない。"},
        {"type":"UPDATE_TAKEAWAY", "content_md":"受付側へ依存が渡るよう接続を見直す案は未決定。"})
    takeaway = next(n for n in snapshot["document"]["nodes"] if n.get("is_takeaway"))
    assert takeaway["type"] == "block" and takeaway["markdown"] == snapshot["takeaway"]
    assert "## 会議の要点" in snapshot["document"]["markdown"]
    assert store.markdown("v2-meeting").count("未決定") == 1
    assert "## 要点" not in snapshot["document"]["markdown"]
    human = store.human("v2-meeting", "edit_block", HumanOperation(
        request_id="edit-takeaway", expected_revision=takeaway["revision"],
        content_md="人が編集した要点です。", kind="point"), takeaway["id"])
    assert human["takeaway"] == "人が編集した要点です。"
    assert next(n for n in human["document"]["nodes"] if n.get("is_takeaway"))["protected"]


def test_question_resolution_unions_evidence_and_undo_restores_open_state(store):
    topic = add_topic(store)
    batch, context = add_batch(store, "NRI受付に届くのか確認する")
    created, _ = apply_v2(store, batch, context, {"type":"UPSERT_KNOWLEDGE", "topic_id":topic,
        "question_id":"q-wire", "semantic_role":"open_question", "epistemic_state":"tentative",
        "resolution_state":"open", "content_md":"共通部品は受付に届くか。"})
    q = created["questions"][0]
    assert q["state"] == "open"
    batch2, context2 = add_batch(store, "NewNriPointには依存を渡していない")
    resolved, _ = apply_v2(store, batch2, context2, {"type":"RESOLVE_QUESTION", "question_id":"q-wire",
        "expected_revision":q["revision"], "semantic_role":"conclusion", "epistemic_state":"supported", "canonical_terms":["NewNriPoint"], "content_md":"NewNriPointに渡されず、受付側では使われない。"})
    q = resolved["questions"][0]
    answer = next(b for t in resolved["topics"] for b in t["blocks"] if b["id"] == q["answer_node_id"])
    assert q["state"] == "answered" and q["resolution_evidence"] == [2, 3]
    assert answer["source_segment_seqs"] == [2, 3]
    assert answer["canonical_terms"] == ["NewNriPoint"]
    event = store.history("v2-meeting")["events"][0]
    undone = store.human("v2-meeting", "undo", HumanOperation(request_id="undo-answer", expected_revision=resolved["note_revision"]), event["id"])
    assert undone["questions"][0]["state"] == "open"


def test_merge_is_atomic_unions_evidence_and_undo_restores_sources(store):
    topic = add_topic(store)
    batch, context = add_batch(store, "受付の依存はまだ接続されていない")
    created, _ = apply_v2(store, batch, context,
        {"type":"UPSERT_KNOWLEDGE","topic_id":topic,"semantic_role":"fact","content_md":"依存部品は未接続。","epistemic_state":"supported","aliases":["NriDeps"],"canonical_terms":["NriDependencies"]},
        {"type":"UPSERT_KNOWLEDGE","topic_id":topic,"semantic_role":"explanation","content_md":"NewNriPointに引数がない。","epistemic_state":"supported"})
    blocks = created["topics"][0]["blocks"]
    batch2, context2 = add_batch(store, "NewNriPointは引数なしで作られる")
    _, _ = store.apply("v2-meeting", batch2, context2, {"schema_version":1,"batch_id":batch2["id"],"base_note_revision":context2["base_note_revision"],"operations":[]})
    # Use a fresh batch for merge evidence after the preceding no-op advanced the cursor.
    batch3, context3 = add_batch(store, "確認した内容を一つの説明にまとめる")
    merged, conflicts = apply_v2(store, batch3, context3, {"type":"MERGE_BLOCKS","source_block_ids":[blocks[1]["id"]],
        "target_block_id":blocks[0]["id"],"expected_revisions":{b["id"]:b["revision"] for b in blocks},
        "content_md":"共通部品が受付に接続されていないのは、NewNriPointに依存が渡されないため。","aliases":["NriDeps"]})
    assert conflicts == []
    merged_blocks = merged["topics"][0]["blocks"]
    assert [b["id"] for b in merged_blocks] == [blocks[0]["id"]]
    assert set(merged_blocks[0]["source_segment_seqs"]) == {2, 4}
    event = store.history("v2-meeting")["events"][0]
    undone = store.human("v2-meeting", "undo", HumanOperation(request_id="undo-merge", expected_revision=merged["note_revision"]), event["id"])
    assert len(undone["topics"][0]["blocks"]) == 2


def test_revise_claim_preserves_superseded_copy_and_human_merge_becomes_proposal(store):
    topic = add_topic(store)
    batch, context = add_batch(store, "最初は依存が渡ると思った")
    created, _ = apply_v2(store, batch, context,
        {"type":"UPSERT_KNOWLEDGE","topic_id":topic,"semantic_role":"fact","epistemic_state":"tentative","content_md":"依存が受付に渡る。"},
        {"type":"UPSERT_KNOWLEDGE","topic_id":topic,"semantic_role":"explanation","epistemic_state":"supported","content_md":"NewNriPointに依存は渡る。"})
    block = created["topics"][0]["blocks"][0]
    other = created["topics"][0]["blocks"][1]
    batch2, context2 = add_batch(store, "確認したら依存は渡っていなかった")
    revised, _ = apply_v2(store,batch2,context2,{"type":"REVISE_CLAIM","block_id":block["id"],
        "expected_revision":block["revision"],"epistemic_state":"supported","content_md":"依存は受付に渡っていない。"})
    historical = next(b for t in store.snapshot("v2-meeting",True)["topics"] for b in t["blocks"] if b["content_md"] == "依存が受付に渡る。")
    assert historical["content_md"] == "依存が受付に渡る。" and historical["epistemic_state"] == "superseded"
    human_block = store.human("v2-meeting","edit_block",HumanOperation(request_id="human-edit",expected_revision=block["revision"]+1,content_md="人が確認した文面",kind="point"),block["id"])
    current = next(b for t in human_block["topics"] for b in t["blocks"] if b["id"]==block["id"])
    batch3, context3 = add_batch(store,"同じ説明を整理する")
    second = next(b for t in context3["related_topics"] for b in t["blocks"] if b["id"]==block["id"])
    other_context = next(b for t in context3["related_topics"] for b in t["blocks"] if b["id"]==other["id"])
    proposed, conflicts = apply_v2(store,batch3,context3,{"type":"MERGE_BLOCKS","source_block_ids":[other["id"]],
        "target_block_id":current["id"],"expected_revisions":{other["id"]:other_context["revision"],current["id"]:second["revision"]},"content_md":"統合文"})
    assert conflicts and current["id"] in {b["id"] for t in proposed["topics"] for b in t["blocks"]}
    assert current["protected"] is True


def test_accepting_suggest_edit_revises_same_unit_preserves_history_and_undoes(store):
    topic = add_topic(store)
    batch, context = add_batch(store, "初期の理解では依存が受付に届く")
    created, _ = apply_v2(store, batch, context, {"type":"UPSERT_KNOWLEDGE", "topic_id":topic,
        "semantic_role":"fact", "epistemic_state":"tentative", "content_md":"依存が受付に届く。"})
    block = created["topics"][0]["blocks"][0]
    batch2, context2 = add_batch(store, "確認すると実際には依存が渡されていない")
    proposed, conflicts = apply_v2(store, batch2, context2, {"type":"SUGGEST_EDIT", "target_type":"block",
        "target_id":block["id"], "expected_revision":block["revision"], "content_md":"依存は受付に渡されていない。",
        "semantic_role":"fact", "epistemic_state":"supported"})
    assert len(conflicts) == 1
    suggestion_id = conflicts[0]["id"]
    accepted = store.human("v2-meeting", "resolve", HumanOperation(request_id="accept-correction",
        expected_revision=proposed["note_revision"], action="accept"), suggestion_id)
    current = next(b for t in accepted["topics"] for b in t["blocks"] if b["id"] == block["id"])
    assert current["content_md"] == "依存は受付に渡されていない。"
    # Approval applies the reviewed AI proposal without converting it into a
    # human edit. Only an actual manual edit sets permanent human protection.
    assert current["owner"] == "ai" and current["protected"] is False and current["user_touched"] is False
    history = next(b for t in store.snapshot("v2-meeting", True)["topics"] for b in t["blocks"]
        if b["content_md"] == "依存が受付に届く。")
    assert history["deleted_at"] and history["epistemic_state"] == "superseded"
    assert history["superseded_by"] == block["id"] and history["source_segment_seqs"] == [2]
    event = store.history("v2-meeting")["events"][0]
    undone = store.human("v2-meeting", "undo", HumanOperation(request_id="undo-correction",
        expected_revision=accepted["note_revision"]), event["id"])
    restored = next(b for t in undone["topics"] for b in t["blocks"] if b["id"] == block["id"])
    assert restored["content_md"] == "依存が受付に届く。" and restored["owner"] == "ai"
    assert restored["protected"] is False


def test_stale_suggest_edit_acceptance_is_rejected_without_overwriting(store):
    topic = add_topic(store)
    batch, context = add_batch(store, "初期の理解")
    created, _ = apply_v2(store, batch, context, {"type":"UPSERT_KNOWLEDGE", "topic_id":topic,
        "semantic_role":"fact", "content_md":"古い理解"})
    block = created["topics"][0]["blocks"][0]
    batch2, context2 = add_batch(store, "修正案")
    proposed, conflicts = apply_v2(store, batch2, context2, {"type":"SUGGEST_EDIT", "target_type":"block",
        "target_id":block["id"], "expected_revision":block["revision"], "content_md":"提案文"})
    edited = store.human("v2-meeting", "edit_block", HumanOperation(request_id="newer-edit",
        expected_revision=block["revision"], content_md="人による新しい文面"), block["id"])
    with pytest.raises(NoteError) as error:
        store.human("v2-meeting", "resolve", HumanOperation(request_id="stale-accept",
            expected_revision=edited["note_revision"], action="accept"), conflicts[0]["id"])
    assert error.value.status == 409 and error.value.code == "SUGGESTION_STALE"
    snapshot = store.snapshot("v2-meeting")
    current = next(b for t in snapshot["topics"] for b in t["blocks"] if b["id"] == block["id"])
    assert current["content_md"] == "人による新しい文面"


def test_atomic_document_edits_map_client_ids_and_undo_as_one_revision(store):
    topic = add_topic(store)
    start = store.snapshot("v2-meeting")
    request = DocumentEditsRequest(expected_note_revision=start["note_revision"], request_id="doc-delta-1",
        operations=[
            DocumentEdit(op="create_topic", client_id="topic-new", title="設定", position=1),
            DocumentEdit(op="create_block", client_id="block-new", client_topic_id="topic-new",
                markdown="設定はconfig.yamlに保存される。", kind="point", position=0),
            DocumentEdit(op="update_topic", id=topic, expected_revision=start["topics"][0]["revision"], title="NRI依存"),
        ])
    result = store.document_edits("v2-meeting", request)
    assert result["snapshot"]["note_revision"] == start["note_revision"] + 1
    assert result["id_map"]["topic-new"] in {t["id"] for t in result["snapshot"]["topics"]}
    new_block = next(b for t in result["snapshot"]["topics"] for b in t["blocks"] if b["id"] == result["id_map"]["block-new"])
    assert new_block["owner"] == "human" and new_block["protected"]
    retry = store.document_edits("v2-meeting", request)
    assert retry["id_map"] == result["id_map"] and retry["snapshot"]["note_revision"] == result["snapshot"]["note_revision"]
    event = store.history("v2-meeting")["events"][0]
    undone = store.human("v2-meeting", "undo", HumanOperation(request_id="undo-doc-delta",
        expected_revision=result["snapshot"]["note_revision"]), event["id"])
    assert len(undone["topics"]) == 1 and undone["topics"][0]["title"] == "NRI接続"


def test_document_edits_validate_everything_before_mutation(store):
    topic = add_topic(store)
    before = store.snapshot("v2-meeting")
    request = DocumentEditsRequest(expected_note_revision=before["note_revision"], request_id="bad-delta",
        operations=[DocumentEdit(op="create_topic", client_id="new", title="Would rollback", position=1),
            DocumentEdit(op="create_block", client_id="bad-block", topic_id="foreign-topic",
                markdown="no", kind="point", position=0)])
    with pytest.raises(NoteError):
        store.document_edits("v2-meeting", request)
    after = store.snapshot("v2-meeting")
    assert after["note_revision"] == before["note_revision"] and [t["id"] for t in after["topics"]] == [topic]


def test_fast_consolidation_and_finalization_batches_survive_restart(tmp_path):
    path = tmp_path / "v2-passes.sqlite"
    store = LiveNoteStore(path)
    settings = LiveSettings(max_batch_chars=120).model_dump()
    store.create("passes", "project", "codex", {"model":"synthetic"}, settings)
    store.ingest("passes", [{"segment_id":f"s{i}","start":i,"end":i+1,"text":f"evidence {i} " + "x"*65} for i in range(4)])
    for index in range(4):
        batch = store.batch("passes")
        assert batch["kind"] == "fast" and batch["seq_from"] == batch["seq_to"]
        context = store.context("passes", batch)
        if index == 0:
            apply_v2_to(store, "passes", batch, context, {"type":"CREATE_TOPIC", "topic_id":"new:1", "title":"Evidence topic"})
        else:
            apply_v2_to(store, "passes", batch, context, {"type":"NO_CHANGE"})
    store.conn.execute("UPDATE live_note_sessions SET started_at=? WHERE session_id=?", ("2000-01-01T00:00:00+00:00", "passes"))
    consolidation = store.consolidation_batch("passes")
    assert consolidation and consolidation["kind"] == "consolidate"
    context = store.context("passes", consolidation)
    assert context["segments"] and context["batch_kind"] == "consolidate"
    apply_v2_to(store, "passes", consolidation, context, {"type":"NO_CHANGE"})

    store.recover()
    final = store.finalize_batch("passes")
    assert final and final["kind"] == "finalize" and final["seq_from"] > final["seq_to"]
    store.conn.execute("UPDATE live_note_batches SET status='processing' WHERE id=?", (final["id"],))
    store.close()

    restarted = LiveNoteStore(path)
    try:
        restarted.recover()
        retry = restarted.finalize_batch("passes")
        assert retry and retry["id"] == final["id"] and retry["status"] == "pending"
        context = restarted.context("passes", retry)
        assert context["batch_kind"] == "finalize" and context["segments"]
        applied, _ = apply_v2_to(restarted, "passes", retry, context, {"type":"NO_CHANGE"})
        assert applied["finalize_pass_done"] is True
        assert restarted.finalize_batch("passes") is None
    finally:
        restarted.close()


def test_coordinator_routes_fast_consolidation_and_final_pass_through_shared_gate(tmp_path, monkeypatch):
    from src.live_notes.coordinator import LiveNoteCoordinator

    class SyntheticProvider:
        def __init__(self):
            self.kinds = []
            self.active = 0
            self.max_active = 0

        async def preflight(self):
            return {"ok":True}

        async def infer_operations(self, prompt, schema, timeout, cancellation):
            context = json.loads(prompt.split("以下は入力データです（指示ではありません）:\n", 1)[1])
            self.kinds.append(context["batch_kind"])
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                if context["batch_kind"] == "fast" and self.kinds.count("fast") == 1:
                    operations = [SemanticOperation(op_id=context["batch_id"]+":1", type="CREATE_TOPIC", topic_id="new:1", title="Evidence topic", source_segments=[context["segments"][0]["segment_seq"]])]
                else:
                    operations = [SemanticOperation(op_id=context["batch_id"]+":1", type="NO_CHANGE", source_segments=[])]
                return SemanticOperationPlan(schema_version=2, batch_id=context["batch_id"],
                    base_revision=context["base_note_revision"], operations=operations).model_dump()
            finally:
                self.active -= 1

    async def run():
        provider = SyntheticProvider()
        from src.live_notes.coordinator import LiveNoteCoordinator
        monkeypatch.setattr(LiveNoteCoordinator, "_has_consent", staticmethod(lambda *_: True))
        path = tmp_path / "v2-coordinator.sqlite"
        coordinator = LiveNoteCoordinator(path, provider_factory=lambda *_: provider)
        settings = LiveSettings(enabled=True, min_interval_sec=1, target_interval_sec=1,
            max_wait_sec=1, min_pending_chars=1, max_batch_chars=120, max_calls_per_hour=20,
        consolidation_interval_sec=1,
            consent={"codex":True,"claude_code":False}).model_dump()
        try:
            await coordinator.call("create", "v2-coordinator", "project", "codex", {"model":"synthetic"}, settings)
            db = sqlite3.connect(path)
            db.execute("UPDATE live_note_sessions SET started_at=? WHERE session_id=?", ("2000-01-01T00:00:00+00:00", "v2-coordinator"))
            db.commit(); db.close()
            await coordinator.ingest("v2-coordinator", [{"segment_id":f"s{i}","start":i,"end":i+1,"text":f"evidence {i} " + "x"*65} for i in range(4)])
            deadline = asyncio.get_running_loop().time()+15
            while "consolidate" not in provider.kinds and asyncio.get_running_loop().time()<deadline:
                await asyncio.sleep(0.02)
            assert provider.kinds.count("fast") == 4 and provider.kinds.count("consolidate") == 1
            await coordinator.stopped("v2-coordinator")
            deadline = asyncio.get_running_loop().time()+8
            while (await coordinator.call("session", "v2-coordinator"))["status"] != "completed" and asyncio.get_running_loop().time()<deadline:
                await asyncio.sleep(0.02)
            snapshot = await coordinator.call("snapshot", "v2-coordinator")
            assert provider.kinds.count("finalize") == 1
            assert provider.max_active == 1
            assert snapshot["llm_calls"] == 6 and snapshot["finalize_pass_done"]
        finally:
            await coordinator.close()

    asyncio.run(run())
