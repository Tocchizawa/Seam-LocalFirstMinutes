"""Transactional regressions for question and suggestion lifecycle behavior."""

import uuid

import pytest

from src.live_notes.models import HumanOperation, LiveSettings, SemanticOperation, SemanticOperationPlan
from src.live_notes.repository import LiveNoteStore, NoteError


@pytest.fixture
def store(tmp_path):
    db = LiveNoteStore(tmp_path / "suggestions.sqlite")
    db.create("suggestion-meeting", "project", "codex", {"model": "synthetic"}, LiveSettings().model_dump())
    yield db
    db.close()


def batch_for(store, text):
    store.ingest("suggestion-meeting", [{"segment_id": uuid.uuid4().hex, "start": 12, "end": 14, "text": text}])
    batch = store.batch("suggestion-meeting")
    return batch, store.context("suggestion-meeting", batch)


def apply(store, batch, context, *operations):
    raw = SemanticOperationPlan(
        schema_version=2, batch_id=batch["id"], base_revision=context["base_revision"], reason="synthetic",
        operations=[SemanticOperation(op_id=f"{batch['id']}:{i}", source_segments=[batch["seq_to"]], **op).model_dump()
                    for i, op in enumerate(operations, 1)],
    ).model_dump()
    return store.apply("suggestion-meeting", batch, context, raw)


def make_question_and_answer_target(store):
    batch, context = batch_for(store, "未解決の問いを確認")
    snapshot, _ = apply(store, batch, context,
        {"type": "CREATE_TOPIC", "topic_id": "new:1", "title": "接続"},
        {"type": "UPSERT_KNOWLEDGE", "topic_id": "new:1", "semantic_role": "fact", "content_md": "既存の確認事項。"},
    )
    topic = snapshot["topics"][0]
    batch, context = batch_for(store, "回答の根拠を確認")
    snapshot, _ = apply(store, batch, context,
        {"type": "UPSERT_KNOWLEDGE", "topic_id": topic["id"], "question_id": "q-lifecycle",
         "semantic_role": "open_question", "resolution_state": "open", "content_md": "依存は受付まで届くか。"},
    )
    question = snapshot["questions"][0]
    block = next(b for t in snapshot["topics"] for b in t["blocks"] if b["semantic_role"] == "fact")
    return topic["id"], question, block


def test_resolve_question_uses_independent_question_and_answer_revisions(store):
    topic_id, question, block = make_question_and_answer_target(store)
    store.conn.execute("UPDATE live_note_questions SET revision=4 WHERE id=?", (question["id"],))
    batch, context = batch_for(store, "依存は受付には渡されない")
    snapshot, conflicts = apply(store, batch, context, {
        "type": "RESOLVE_QUESTION", "topic_id": topic_id, "block_id": block["id"],
        "question_id": question["id"], "expected_revision": 4,
        "expected_revisions": {block["id"]: block["revision"]},
        "semantic_role": "conclusion", "content_md": "依存は受付には渡されない。",
    })
    assert not conflicts
    assert next(q for q in snapshot["questions"] if q["id"] == question["id"])["state"] == "answered"
    assert next(b for t in snapshot["topics"] for b in t["blocks"] if b["id"] == block["id"])["content_md"] == "依存は受付には渡されない。"


def test_answered_question_repeat_is_idempotent_and_new_answer_is_targeted_proposal(store):
    topic_id, question, _ = make_question_and_answer_target(store)
    batch, context = batch_for(store, "実際には届かない")
    snapshot, _ = apply(store, batch, context, {
        "type": "RESOLVE_QUESTION", "topic_id": topic_id, "question_id": question["id"],
        "expected_revision": question["revision"], "semantic_role": "conclusion",
        "content_md": "依存は受付には渡されない。",
    })
    answer_id = next(q for q in snapshot["questions"] if q["id"] == question["id"])["answer_node_id"]

    batch, context = batch_for(store, "別の発話も同じ答えを裏付ける")
    snapshot, conflicts = apply(store, batch, context, {
        "type": "RESOLVE_QUESTION", "topic_id": topic_id, "question_id": question["id"],
        "expected_revision": 1, "semantic_role": "conclusion", "content_md": "依存は受付には渡されない。",
    })
    answer = next(b for t in snapshot["topics"] for b in t["blocks"] if b["id"] == answer_id)
    assert not conflicts and answer["source_segment_seqs"] == [2, 3, 4]
    assert snapshot["suggestions"] == []

    batch, context = batch_for(store, "新しい説明が加わる")
    snapshot, conflicts = apply(store, batch, context, {
        "type": "RESOLVE_QUESTION", "topic_id": topic_id, "question_id": question["id"],
        "expected_revision": 2, "semantic_role": "conclusion",
        "content_md": "依存は受付には渡されず、別の初期化処理で保持される。",
    })
    assert not conflicts and snapshot["suggestions"] == []
    answer = next(b for t in snapshot["topics"] for b in t["blocks"] if b["id"] == answer_id)
    assert answer["content_md"].endswith("別の初期化処理で保持される。")
    assert answer["owner"] == "ai" and answer["protected"] is False and answer["user_touched"] is False
    history = next(b for t in store.snapshot("suggestion-meeting", True)["topics"] for b in t["blocks"] if b["id"] != answer_id and b["content_md"] == "依存は受付には渡されない。")
    assert history["epistemic_state"] == "superseded" and history["superseded_by"] == answer_id


def test_protected_answer_replay_suggests_against_answer_and_preserves_human_content(store):
    topic_id, question, _ = make_question_and_answer_target(store)
    batch, context = batch_for(store, "回答")
    snapshot, _ = apply(store, batch, context, {
        "type": "RESOLVE_QUESTION", "topic_id": topic_id, "question_id": question["id"],
        "expected_revision": question["revision"], "semantic_role": "conclusion", "content_md": "既存回答。",
    })
    q = next(q for q in snapshot["questions"] if q["id"] == question["id"])
    answer = next(b for t in snapshot["topics"] for b in t["blocks"] if b["id"] == q["answer_node_id"])
    snapshot = store.human("suggestion-meeting", "edit_block",
        HumanOperation(request_id="human-answer", expected_revision=answer["revision"], content_md="人が確認した回答。"), answer["id"])
    batch, context = batch_for(store, "回答を更新する追加根拠")
    snapshot, _ = apply(store, batch, context, {
        "type": "RESOLVE_QUESTION", "topic_id": topic_id, "question_id": question["id"],
        "expected_revision": q["revision"], "semantic_role": "conclusion", "content_md": "追加情報を含む回答。",
    })
    suggestion = snapshot["suggestions"][0]
    assert suggestion["presentation"]["reason_code"] == "user_edit"
    assert suggestion["presentation"]["current_md"] == "人が確認した回答。"
    assert suggestion["presentation"]["source_segments"][0]["text"] == "回答を更新する追加根拠"


def test_pending_suggestions_deduplicate_supersede_and_stale_accept_is_hidden(store):
    topic_id, _, block = make_question_and_answer_target(store)
    # Turn the target into a human protected block for a real SUGGEST_EDIT conflict.
    current = store.snapshot("suggestion-meeting")
    block = next(b for t in current["topics"] for b in t["blocks"] if b["id"] == block["id"])
    store.human("suggestion-meeting", "edit_block", HumanOperation(request_id="protect-target", expected_revision=block["revision"], content_md="手動編集。"), block["id"])
    for i, text in enumerate(("提案A", "提案A", "提案B")):
        batch, context = batch_for(store, f"新しい根拠 {i}")
        snapshot, _ = apply(store, batch, context, {
            "type": "SUGGEST_EDIT", "target_type": "block", "target_id": block["id"],
            "expected_revision": block["revision"], "semantic_role": "fact", "content_md": text,
        })
    assert len(snapshot["suggestions"]) == 1
    suggestion = snapshot["suggestions"][0]
    assert suggestion["presentation"]["proposed_md"] == "提案B"
    assert suggestion["presentation"]["source_segments"][-1]["text"] == "新しい根拠 2"

    # A manual edit after proposal makes the proposal stale; acceptance safely
    # supersedes it and never applies stale AI wording to the current block.
    live = next(b for t in snapshot["topics"] for b in t["blocks"] if b["id"] == block["id"])
    snapshot = store.human("suggestion-meeting", "edit_block", HumanOperation(request_id="later-edit", expected_revision=live["revision"], content_md="さらに手動編集。"), live["id"])
    with pytest.raises(NoteError) as error:
        store.human("suggestion-meeting", "resolve", HumanOperation(request_id="accept-stale", expected_revision=snapshot["note_revision"], action="accept"), suggestion["id"])
    assert error.value.status == 409 and error.value.code == "SUGGESTION_STALE"
    snapshot = store.snapshot("suggestion-meeting")
    assert snapshot["suggestions"] == []
    current = next(b for t in snapshot["topics"] for b in t["blocks"] if b["id"] == block["id"])
    assert current["content_md"] == "さらに手動編集。" and current["owner"] == "human"
    status = store.conn.execute("SELECT status FROM live_note_suggestions WHERE id=?", (suggestion["id"],)).fetchone()[0]
    assert status == "superseded"


def test_invalid_late_operation_rolls_back_suggestion_and_evidence_cursor(store):
    batch, context = batch_for(store, "前段の候補")
    operations = [
        SemanticOperation(op_id=f"{batch['id']}:1", type="CREATE_TOPIC", title="仮の論点", topic_id="new:1", source_segments=[batch["seq_to"]]),
        SemanticOperation(op_id=f"{batch['id']}:2", type="UPSERT_KNOWLEDGE", topic_id="new:1", semantic_role="fact", content_md="存在しない根拠を参照", source_segments=[999]),
    ]
    raw = SemanticOperationPlan(schema_version=2, batch_id=batch["id"], base_revision=context["base_revision"], reason="rollback", operations=operations).model_dump()
    with pytest.raises(NoteError):
        store.apply("suggestion-meeting", batch, context, raw)
    snapshot = store.snapshot("suggestion-meeting")
    assert snapshot["topics"] == [] and snapshot["suggestions"] == []
    assert snapshot["last_processed_segment_seq"] == 0
    assert store.batch("suggestion-meeting")["status"] != "done"


def test_migration_supersedes_legacy_pending_resolution_for_closed_question(store):
    _, question, _ = make_question_and_answer_target(store)
    batch, context = batch_for(store, "回答済み")
    snapshot, _ = apply(store, batch, context, {
        "type": "RESOLVE_QUESTION", "question_id": question["id"], "expected_revision": question["revision"],
        "semantic_role": "conclusion", "content_md": "回答済みの内容。",
    })
    closed = next(q for q in snapshot["questions"] if q["id"] == question["id"])
    store.conn.execute(
        "INSERT INTO live_note_suggestions VALUES(?,?,?,?,?,?,?,?)",
        ("legacy-dead-end", "suggestion-meeting", "topic", closed["topic_id"],
         '{"type":"RESOLVE_QUESTION","question_id":"q-lifecycle","expected_revision":0,"content_md":"旧案"}',
         "旧い回答提案", "pending", "2026-01-01T00:00:00+00:00"),
    )
    from src.live_notes.repository import migrate
    migrate(store.conn)
    assert store.conn.execute("SELECT status FROM live_note_suggestions WHERE id='legacy-dead-end'").fetchone()[0] == "superseded"
    assert all(item["id"] != "legacy-dead-end" for item in store.snapshot("suggestion-meeting")["suggestions"])


def test_stale_ai_answer_candidate_is_retained_against_current_revision(store):
    topic_id, question, _ = make_question_and_answer_target(store)
    batch, context = batch_for(store, "最初の回答")
    snapshot, _ = apply(store, batch, context, {
        "type": "RESOLVE_QUESTION", "topic_id": topic_id, "question_id": question["id"],
        "expected_revision": question["revision"], "semantic_role": "conclusion", "content_md": "最初の回答。",
    })
    q = next(q for q in snapshot["questions"] if q["id"] == question["id"])
    answer_id = q["answer_node_id"]
    batch, context = batch_for(store, "新しい説明候補")
    store.conn.execute("UPDATE live_note_blocks SET content_md='並行更新',revision=revision+1 WHERE id=?", (answer_id,))
    snapshot, conflicts = apply(store, batch, context, {
        "type": "RESOLVE_QUESTION", "topic_id": topic_id, "question_id": question["id"],
        "expected_revision": q["revision"], "semantic_role": "conclusion", "content_md": "新しい説明を含む回答。",
    })
    assert len(conflicts) == 1
    proposal = snapshot["suggestions"][0]
    assert proposal["target_id"] == answer_id
    assert proposal["presentation"]["current_md"] == "並行更新"
    assert proposal["presentation"]["proposed_md"] == "新しい説明を含む回答。"
    assert proposal["presentation"]["reason_code"] == "newer_ai_update"
    assert proposal["presentation"]["summary"] != proposal["presentation"]["proposed_md"]
    assert proposal["presentation"]["source_segments"][0]["text"] == "新しい説明候補"


def test_stale_open_question_fails_batch_without_advancing_evidence_cursor(store):
    _, question, _ = make_question_and_answer_target(store)
    batch, context = batch_for(store, "遅れて解釈された回答根拠")
    store.conn.execute("UPDATE live_note_questions SET revision=revision+1 WHERE id=?", (question["id"],))
    with pytest.raises(NoteError) as error:
        apply(store, batch, context, {
            "type": "RESOLVE_QUESTION", "question_id": question["id"],
            "expected_revision": question["revision"], "semantic_role": "conclusion", "content_md": "回答候補。",
        })
    assert error.value.code == "REVISION_CONFLICT"
    snapshot = store.snapshot("suggestion-meeting")
    assert snapshot["last_processed_segment_seq"] == batch["seq_from"] - 1
    assert snapshot["suggestions"] == []
    assert store.batch("suggestion-meeting")["status"] != "done"


def test_topic_suggestion_presentation_has_current_topic_markdown_and_short_summary(store):
    topic_id, _, _ = make_question_and_answer_target(store)
    batch, context = batch_for(store, "論点への提案")
    snapshot, _ = apply(store, batch, context, {
        "type": "SUGGEST_EDIT", "target_type": "topic", "target_id": topic_id,
        "expected_revision": 0, "semantic_role": "fact", "content_md": "新しい候補の詳細な文面です。",
    })
    suggestion = snapshot["suggestions"][0]
    presentation = suggestion["presentation"]
    assert presentation["target_title"] == "接続"
    assert presentation["current_md"].startswith("## 接続")
    assert "既存の確認事項。" in presentation["current_md"]
    assert presentation["proposed_md"] == "新しい候補の詳細な文面です。"
    assert presentation["summary"] == "「接続」への知見追加を提案"
    assert presentation["summary"] != presentation["proposed_md"]
