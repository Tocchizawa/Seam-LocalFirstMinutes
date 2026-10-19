"""Synthetic contract coverage for additive Live Notes v3 backend behavior."""
import json
import sqlite3
import uuid

import pytest

from src.live_notes.models import DocumentEditsRequest, HumanOperation, LiveSettings, SemanticOperation, SemanticOperationPlan, semantic_operation_plan_schema
from src.live_notes.repository import LiveNoteStore, NoteError, SCHEMA, migrate
from pydantic import ValidationError


@pytest.fixture
def store(tmp_path):
    db = LiveNoteStore(tmp_path / "v3.sqlite")
    db.create("v3-session", "project", "codex", {"model":"synthetic"}, LiveSettings().model_dump())
    yield db
    db.close()


def batch_for(store, text, kind=None):
    store.ingest("v3-session", [{"segment_id":uuid.uuid4().hex,"start":0,"end":1,"text":text}])
    batch=store.batch("v3-session")
    if kind:
        store.conn.execute("UPDATE live_note_batches SET kind=? WHERE id=?", (kind,batch["id"]))
        batch["kind"]=kind
    return batch,store.context("v3-session",batch)


def apply(store,batch,context,*operations,version=3):
    ops=[]
    for i,item in enumerate(operations,1):
        evidence = [batch["seq_to"]] if batch["seq_to"]>=batch["seq_from"] else [context["segments"][-1]["segment_seq"]] if context["segments"] else []
        ops.append(SemanticOperation(op_id=f"{batch['id']}:{i}",source_segments=evidence,**item))
    plan=SemanticOperationPlan(schema_version=version,batch_id=batch["id"],base_revision=context["base_revision"],reason="synthetic",operations=ops).model_dump()
    return store.apply("v3-session",batch,context,plan)


def test_v3_migration_is_idempotent_and_rollback_safe(store):
    store.ingest("v3-session", [{"segment_id":"legacy","start":0,"end":1,"text":"preserve this segment"}])
    before=store.conn.execute("SELECT text FROM live_note_segments").fetchall()
    migrate(store.conn)
    migrate(store.conn)
    assert store.conn.execute("SELECT text FROM live_note_segments").fetchall()==before
    assert store.conn.execute("SELECT max(version) FROM live_note_schema").fetchone()[0]==3
    assert {r[1] for r in store.conn.execute("PRAGMA table_info(live_note_questions)")} >= {"conversation_state","verification_state"}

    failing=sqlite3.connect(":memory:")
    failing.row_factory=sqlite3.Row
    failing.executescript(SCHEMA)
    def deny_v3_relation(action,arg1,arg2,db,trigger):
        if action==sqlite3.SQLITE_CREATE_TABLE and arg1=="live_note_item_relations":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    failing.set_authorizer(deny_v3_relation)
    with pytest.raises(sqlite3.DatabaseError):
        migrate(failing)
    assert "canonical_key" not in {r[1] for r in failing.execute("PRAGMA table_info(live_note_blocks)")}
    failing.close()


def test_v3_migration_does_not_reset_question_states_on_reopen(store):
    batch,ctx=batch_for(store,"Which protocol applies?")
    apply(store,batch,ctx,
        {"type":"CREATE_TOPIC","topic_id":"new:1","title":"Protocol"},
        {"type":"UPSERT_KNOWLEDGE","topic_id":"new:1","semantic_role":"open_question","question_id":"q-state","content_md":"Which protocol applies?"})
    store.conn.execute("UPDATE live_note_questions SET conversation_state='reopened',verification_state='needs_verification' WHERE id='q-state'")
    migrate(store.conn)
    q=store.conn.execute("SELECT conversation_state,verification_state FROM live_note_questions WHERE id='q-state'").fetchone()
    assert tuple(q)==("reopened","needs_verification")


def test_question_answer_state_stays_separate_from_external_verification(store):
    batch,ctx=batch_for(store,"What is the cancellation key?")
    result,_=apply(store,batch,ctx,
        {"type":"CREATE_TOPIC","topic_id":"new:1","title":"Cancellation"},
        {"type":"UPSERT_KNOWLEDGE","topic_id":"new:1","semantic_role":"open_question","resolution_state":"open","question_id":"q1","content_md":"Which member key is used for cancellation?"})
    question=store.snapshot("v3-session")["questions"][0]
    batch,ctx=batch_for(store,"Discussion says to use the order-time EPID.")
    apply(store,batch,ctx,{"type":"RESOLVE_QUESTION","question_id":"q1","expected_revision":question["revision"],"content_md":"Use the EPID recorded with the order.","semantic_role":"conclusion"})
    question=store.snapshot("v3-session")["questions"][0]
    assert question["conversation_state"]=="answered_in_discussion"
    assert question["verification_state"]=="unverified"
    assert store.snapshot("v3-session")["document"]["unresolved_count"]==0


def test_cross_topic_search_and_revisioned_document_title(store):
    for index in range(7):
        batch,ctx=batch_for(store,f"Topic {index} discussion")
        title="Legacy EPID cancellation" if index==0 else f"Unrelated topic {index}"
        apply(store,batch,ctx,
            {"type":"CREATE_TOPIC","topic_id":"new:1","title":title},
            {"type":"UPSERT_KNOWLEDGE","topic_id":"new:1","semantic_role":"explanation","content_md":"Use order-time EPID for cancellation." if index==0 else f"Unrelated detail {index}."})
    batch,ctx=batch_for(store,"EPID cancellation")
    context=store.context("v3-session",batch)
    assert any(t["title"]=="Legacy EPID cancellation" and t["blocks"] for t in context["related_topics"])
    snapshot=store.snapshot("v3-session")
    edited=store.document_edits("v3-session",DocumentEditsRequest(expected_note_revision=snapshot["note_revision"],request_id="title-1",operations=[{"op":"update_document_title","expected_revision":0,"title":"EPID lifecycle"}]))
    assert edited["snapshot"]["document"]["title"]=="EPID lifecycle"
    assert edited["snapshot"]["document"]["title_revision"]==1


def test_human_deleted_meaning_is_not_resurrected_by_identical_evidence(store):
    batch,ctx=batch_for(store,"Keep the existing item for this discussion")
    created,_=apply(store,batch,ctx,
        {"type":"CREATE_TOPIC","topic_id":"new:1","title":"Topic"},
        {"type":"UPSERT_KNOWLEDGE","topic_id":"new:1","semantic_role":"fact","content_md":"The order keeps the original member key."})
    topic=created["topics"][0]
    block=topic["blocks"][0]
    store.document_edits("v3-session",DocumentEditsRequest(expected_note_revision=created["note_revision"],request_id="delete-1",operations=[{"op":"delete_block","id":block["id"],"expected_revision":block["revision"]}]))
    batch,ctx=batch_for(store,"The order keeps the original member key.")
    result,_=apply(store,batch,ctx,{"type":"UPSERT_KNOWLEDGE","topic_id":topic["id"],"semantic_role":"fact","content_md":"The order keeps the original member key."})
    assert not any(b["content_md"]=="The order keeps the original member key." and not b["deleted_at"] for t in result["topics"] for b in t["blocks"])


def test_context_expansion_only_reads_explicit_truncated_ids_within_budget(store):
    batch,ctx=batch_for(store,"technical explanation")
    content="Order-time member key. " + ("detail " * 500)
    result,_=apply(store,batch,ctx,
        {"type":"CREATE_TOPIC","topic_id":"new:1","title":"Order identity"},
        {"type":"UPSERT_KNOWLEDGE","topic_id":"new:1","semantic_role":"explanation","content_md":content})
    block=result["topics"][0]["blocks"][0]
    batch,ctx=batch_for(store,"order-time member key")
    context=store.context("v3-session",batch)
    assert context["context_truncated"]
    expanded=store.expand_context("v3-session",context,{"block_ids":[block["id"]],"topic_ids":[],"question_ids":[],"evidence_segment_seqs":[]})
    full=next(b for t in expanded["related_topics"] for b in t["blocks"] if b["id"]==block["id"])
    assert len(full["content_md"])==len(content)


def test_context_expansion_includes_requested_older_evidence(store):
    first_batch, _ = batch_for(store, "The original explanation names the order-time key.")
    first_seq = first_batch["seq_to"]
    store.conn.execute("UPDATE live_note_batches SET status='done' WHERE id=?", (first_batch["id"],))
    store.conn.execute("UPDATE live_note_sessions SET last_processed_segment_seq=? WHERE session_id='v3-session'", (first_seq,))
    batch, context = batch_for(store, "A later turn asks which key was meant.")
    assert first_seq not in {segment["segment_seq"] for segment in context["segments"]}
    context["context_truncated"] = True
    expanded = store.expand_context("v3-session", context, {"block_ids": [], "topic_ids": [], "question_ids": [], "evidence_segment_seqs": [first_seq]})
    evidence = next(segment for segment in expanded["segments"] if segment["segment_seq"] == first_seq)
    assert evidence["text"] == "The original explanation names the order-time key."
    assert evidence["context_only"] is True
    assert len(json.dumps(expanded, ensure_ascii=False)) <= 36000


def test_manual_consolidation_without_segments_and_fast_topic_merge_rejected(store):
    batch,ctx=batch_for(store,"two topics")
    apply(store,batch,ctx,
        {"type":"CREATE_TOPIC","topic_id":"new:1","title":"First"},
        {"type":"CREATE_TOPIC","topic_id":"new:2","title":"Second"})
    snap=store.snapshot("v3-session")
    a,b=[t["id"] for t in snap["topics"]]
    batch,ctx=batch_for(store,"merge them")
    with pytest.raises(NoteError):
        apply(store,batch,ctx,{"type":"MERGE_TOPICS","topic_id":a,"source_topic_id":b,"expected_topic_revisions":{a:0,b:0}})
    store.conn.execute("UPDATE live_note_batches SET status='done' WHERE id=?",(batch["id"],))
    store.conn.execute("UPDATE live_note_sessions SET last_processed_segment_seq=? WHERE session_id='v3-session'",(batch["seq_to"],))
    manual=store.manual_consolidation_batch("v3-session")
    assert manual and manual["kind"]=="consolidate"
    assert store.snapshot("v3-session")["pending_segments"]==0
    context=store.context("v3-session",manual)
    missing_evidence=SemanticOperationPlan(schema_version=3,batch_id=manual["id"],base_revision=context["base_revision"],operations=[SemanticOperation(op_id=manual["id"]+":missing",type="MERGE_TOPICS",topic_id=a,source_topic_id=b,expected_topic_revisions={a:0,b:0},source_segments=[])]).model_dump()
    with pytest.raises(NoteError, match="根拠"):
        store.apply("v3-session",manual,context,missing_evidence)
    result,_=apply(store,manual,context,{"type":"MERGE_TOPICS","topic_id":a,"source_topic_id":b,"expected_topic_revisions":{a:0,b:0}})
    assert next(t for t in store.snapshot("v3-session",include_deleted=True)["topics"] if t["id"]==b)["archived_at"]
    assert store.conn.execute("SELECT relation_type FROM live_note_item_relations WHERE session_id='v3-session'").fetchone()[0]=="duplicates"


def test_v3_schema_adds_only_two_operations_and_keeps_v2_parser_compatibility():
    schema=semantic_operation_plan_schema(version=3)
    assert schema["properties"]["schema_version"]["enum"]==[3]
    for version in (2,3):
        assert SemanticOperationPlan(schema_version=version,batch_id="b",base_revision=0,operations=[])
    with pytest.raises(ValidationError):
        SemanticOperation(op_id="x",type="SET_ITEM_STATUS",block_id="b",expected_revision=0)
    with pytest.raises(ValidationError):
        SemanticOperationPlan(schema_version=2,batch_id="b",base_revision=0,operations=[
            SemanticOperation(op_id="b:1",type="UPSERT_KNOWLEDGE",topic_id="t",semantic_role="fact",content_md="x",relations=[{"target_id":"other","relation_type":"supports","expected_revision":0}])
        ])


def test_topic_digest_persists_and_drives_outline_and_search(store):
    batch,ctx=batch_for(store,"discuss unrelated phrase")
    apply(store,batch,ctx,
        {"type":"CREATE_TOPIC","topic_id":"new:1","title":"General","topic_digest":"ultraviolet protocol governs the appeal window"},
        {"type":"UPSERT_KNOWLEDGE","topic_id":"new:1","semantic_role":"explanation","content_md":"A generic note."})
    topic=store.snapshot("v3-session")["topics"][0]
    assert topic["digest"]=="ultraviolet protocol governs the appeal window"
    batch,ctx=batch_for(store,"ultraviolet protocol")
    context=store.context("v3-session",batch)
    assert context["outline"][0]["digest"]==topic["digest"]
    assert context["related_topics"][0]["id"]==topic["id"]
    db_path=store.conn.execute("PRAGMA database_list").fetchone()[2]
    store.close()
    reopened=LiveNoteStore(db_path)
    try:
        assert reopened.snapshot("v3-session")["topics"][0]["digest"]==topic["digest"]
    finally:
        reopened.close()


def test_relation_proposals_are_revision_checked_and_undoable(store):
    batch,ctx=batch_for(store,"An initial stable observation")
    first,_=apply(store,batch,ctx,
        {"type":"CREATE_TOPIC","topic_id":"new:1","title":"Observations"},
        {"type":"UPSERT_KNOWLEDGE","topic_id":"new:1","semantic_role":"fact","content_md":"The initial stable observation is recorded."})
    original=first["topics"][0]["blocks"][0]
    batch,ctx=batch_for(store,"A second observation supports the first")
    with pytest.raises(NoteError):
        apply(store,batch,ctx,
            {"type":"UPSERT_KNOWLEDGE","topic_id":first["topics"][0]["id"],"semantic_role":"fact","content_md":"A second observation supports the first.","relations":[{"target_id":original["id"],"relation_type":"supports","expected_revision":original["revision"]+1}]})
    second,_=apply(store,batch,ctx,
        {"type":"UPSERT_KNOWLEDGE","topic_id":first["topics"][0]["id"],"semantic_role":"fact","content_md":"A second observation supports the first.","relations":[{"target_id":original["id"],"relation_type":"supports","expected_revision":original["revision"]}]})
    assert any(r["relation_type"]=="supports" and r["to_item_id"]==original["id"] for r in second["relations"])
    event=store.conn.execute("SELECT id FROM live_note_events WHERE session_id='v3-session' ORDER BY note_revision DESC LIMIT 1").fetchone()[0]
    current=store.snapshot("v3-session")
    store.human("v3-session","undo",HumanOperation(request_id="undo-rel",expected_revision=current["note_revision"]),event)
    assert not any(r["relation_type"]=="supports" for r in store.snapshot("v3-session")["relations"])


def test_existing_question_answer_and_question_status_use_independent_revisions(store):
    batch,ctx=batch_for(store,"What date was agreed?")
    created,_=apply(store,batch,ctx,
        {"type":"CREATE_TOPIC","topic_id":"new:1","title":"Date"},
        {"type":"UPSERT_KNOWLEDGE","topic_id":"new:1","semantic_role":"open_question","question_id":"q-revisions","content_md":"What date was agreed?"})
    question=created["questions"][0]
    block=next(b for t in created["topics"] for b in t["blocks"] if b["id"]==question["question_node_id"])
    batch,ctx=batch_for(store,"The meeting discussed dates")
    with pytest.raises(ValidationError):
        apply(store,batch,ctx,{"type":"RESOLVE_QUESTION","question_id":"q-revisions","block_id":block["id"],"expected_revision":question["revision"],"content_md":"October 18.","expected_revisions":{}})
    with pytest.raises(NoteError):
        apply(store,batch,ctx,{"type":"SET_ITEM_STATUS","block_id":block["id"],"expected_revision":block["revision"],"question_id":"q-revisions","expected_question_revision":question["revision"]+1,"conversation_state":"deferred"})
    resolved,_=apply(store,batch,ctx,{"type":"SET_ITEM_STATUS","block_id":block["id"],"expected_revision":block["revision"],"question_id":"q-revisions","expected_question_revision":question["revision"],"conversation_state":"deferred"})
    latest=next(q for q in resolved["questions"] if q["id"]=="q-revisions")
    assert latest["conversation_state"]=="deferred"
    assert latest["verification_state"]=="unverified"


def test_status_values_are_role_specific_and_verified_requires_evidence():
    with pytest.raises(ValidationError):
        SemanticOperation(op_id="x",type="SET_ITEM_STATUS",block_id="b",expected_revision=0,item_status="verified_against_source")
    with pytest.raises(ValidationError):
        SemanticOperation(op_id="x",type="SET_ITEM_STATUS",block_id="b",expected_revision=0,conversation_state="answered_in_discussion")
    with pytest.raises(ValidationError):
        SemanticOperation(op_id="x",type="SET_ITEM_STATUS",block_id="b",expected_revision=0,question_id="q",expected_question_revision=0,conversation_state="answered_in_discussion")
    with pytest.raises(ValidationError):
        SemanticOperation(op_id="x",type="SET_ITEM_STATUS",block_id="b",expected_revision=0,question_id="q",expected_question_revision=0,verification_state="verified_against_source")


def test_scheduled_consolidation_requires_persisted_semantic_trigger_and_prioritizes_speech(store):
    for index in range(4):
        batch,ctx=batch_for(store,f"No semantic change {index}")
        apply(store,batch,ctx)
    store.conn.execute("UPDATE live_note_sessions SET started_at='2020-01-01T00:00:00+00:00' WHERE session_id='v3-session'")
    assert store.consolidation_batch("v3-session") is None
    batch,ctx=batch_for(store,"A new topic is now in focus")
    apply(store,batch,ctx,{"type":"CREATE_TOPIC","topic_id":"new:1","title":"New focus"})
    persisted=store.conn.execute("SELECT consolidation_reason FROM live_note_batches WHERE id=?",(batch["id"],)).fetchone()[0]
    assert "topic_switch" in json.loads(persisted)["triggers"]
    # Ingested, unprocessed speech remains ahead of automatic structure work.
    store.ingest("v3-session",[{"segment_id":"pending-priority","start":5,"end":6,"text":"Unprocessed speech"}])
    assert store.consolidation_batch("v3-session") is None
    pending=store.batch("v3-session")
    ctx=store.context("v3-session",pending)
    apply(store,pending,ctx)
    ready=store.consolidation_batch("v3-session")
    assert ready and ready["kind"]=="consolidate"

