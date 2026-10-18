# Seam Live Notes v2 backend notes

## Contract and compatibility

- SQLite remains canonical for topics and knowledge blocks. V1 tables and manual APIs remain in place; startup migration adds semantic metadata, first-class question rows, batch kind, and finalization state without rewriting minutes or transcript content.
- V2 CLI output uses a separate strict semantic schema (schema_version 2) with the ten required types. The v1 plan model/schema remains available for replay and compatibility tests.
- Snapshot keeps topics[].blocks[] and adds stable ordered document.nodes, document.markdown, document_revision, questions, and takeaway. Topic headings and blocks keep their existing IDs and per-entity revisions. `UPDATE_TAKEAWAY` creates or updates a normal `会議の要点` topic/block; `snapshot.takeaway` derives from this same active block, so the UI uses the standard editable node and human protection. Legacy metadata takeaways remain readable as a migration fallback. Human block edits set owner, user_touched, and protected. `POST /{session}/document/edits` accepts atomic multi-unit deltas, validates all entity revisions and duplicate writes first, and returns `{snapshot,id_map}` for client-created stable IDs.
- V2 semantic blocks render as ordinary Markdown prose. A persisted `semantic_v2` marker distinguishes these from migrated v1 blocks, which retain their legacy labels on export for old-document compatibility.

## State and safety

- Each accepted plan runs in one SQLite transaction and increments one note revision. Operation IDs are persisted with the batch; replays of completed batches return the existing snapshot.
- Source segment references must belong to the batch/context. Question resolution stores the answer node and evidence union atomically; Undo restores question state and source node metadata. Claim revision retains an archived superseded copy with its evidence. Merge validates all IDs, owner/protection, same-topic membership, and exact input revisions before changing any block; safe merges union evidence, aliases, and canonical terms.
- AI writes to human-owned/protected content and risky merges or restructuring become suggestions. Suggestions can be dismissed or accepted through the existing resolve route. Accepting a text correction updates the same stable block, archives its prior wording/evidence as superseded history, and marks the revised block human-owned/protected. The target revision is checked again during acceptance; stale suggestions return 409. Undo restores the prior content, owner, evidence, and protection state.
- Fast, periodic consolidation, and finalization batches share the existing single coordinator inference semaphore, CLI restrictions, retry policy, and hourly call budget. `consolidation_interval_sec` (default 240 seconds) and `min_consolidation_batches` (default 4) are validated LiveSettings fields. An unchanged-document digest prevents repeat paid consolidation calls until canonical content changes; pending consolidation batches remain claimable before the interval elapses. A stopped batch includes pending segments and doubles as the final pass; if fast processing finished before Stop, a separate durable finalization batch runs, including when its segment cursor is empty. Finalization state survives restart.
- Runtime rejects `verified_against_source=true`: transcript evidence alone cannot establish external source verification.
- CLI execution remains local and restricted through the existing Codex/Claude live adapters. This implementation worker made no model calls. The verifier used four Codex synthetic-only calls before the parent authorized three more; no additional calls were made by this implementation worker.

## Assumptions and limits

- source_segments are sequence IDs within a session. Final/consolidation passes can cite recent saved segments and may reuse already attached evidence, but do not receive an unlimited transcript dump.
- Question records are session-scoped and topic-scoped. Alias/canonical-term values are evidence-backed strings, not graph edges, so alias cycles cannot form.
- RESTRUCTURE_TOPIC applies small AI-owned order/title changes; decisions, risks, proposals, and human-owned structures become proposals. Multi-topic archiving happens only after accepted moves leave the source topic empty.
- Existing handoff's sample path is docs/SEAM_LIVE_NOTES_V2_GOLDEN_SAMPLE.md; GOLDEN_SAMPLE.md does not exist at the handoff root.
- Real CLI validation is opt-in and synthetic-only: SEAM_LIVE_NOTES_V2_REAL_CLI=1 .venv/bin/pytest -q tests/test_live_notes_v2_real_cli.py. It preflights local restrictions, reads the saved provider/model setting, sends one synthetic segment, does one inference per provider per test process, and retries nothing. Keep total calls within the parent-authorized maximum of four for Codex and four for Claude.

## Verification run

- .venv/bin/python -m py_compile src/live_notes/models.py src/live_notes/repository.py src/live_notes/coordinator.py src/live_notes/renderer.py src/live_notes/prompts.py src/api/live_notes.py tests/test_live_notes_v2_backend.py tests/test_live_notes_v2_real_cli.py
- Initial full verifier run exposed two runtime defects: a document-digest SQL reference to a nonexistent block column prevented inference calls from starting, and a stopped final pass compared pending segments against its synthetic empty-range endpoint. Both were corrected. A related scheduler race that hid an already-created consolidation batch until its interval elapsed was fixed by returning the pending batch before interval checks.
- A captured synthetic 12-minute provider response is retained at `tests/fixtures/v2/model-response-canonical-terms.json` and checked against both Pydantic runtime validation and the provider-facing schema. This drove strict empty constraints for unused collections and explicit canonical-term support for answered questions and takeaways.
- A captured synthetic final response is retained at `tests/fixtures/v2/model-response-forward-topic-ref.json` and replayed unchanged against a matching durable batch. V2 application resolves all plan-local `new:N` topics before processing operations, so a valid upsert may refer to a topic whose `CREATE_TOPIC` operation appears later in the plan.
- The opt-in real CLI case was not run by this implementation worker; real inference belongs to the verifier's bounded synthetic-only run.

## Verification evidence

- `.venv/bin/pytest -q tests/test_live_notes_v2_backend.py tests/test_live_notes_v2_real_cli.py`: 30 passed, 2 skipped.
- After the final response replay regression was added, the same V2 command passed 31 tests, 2 skipped; the captured replay is included in this total.
- The captured final response now replays as a nonempty final batch (seq 11–15), advances the cursor to 15, sets `finalize_pass_done`, clears pending segments, and prevents another finalize batch. The empty-range restart path and coordinator integration also passed in the targeted 3-test check.
- `.venv/bin/pytest -q tests/test_live_notes_v2_backend.py tests/test_live_notes_v2_real_cli.py tests/test_live_notes_integration.py tests/test_live_notes_lifecycle_review.py tests/test_live_notes_models.py tests/test_live_notes_providers.py tests/test_live_notes_scheduler.py tests/test_live_notes_schema.py tests/test_live_notes_store.py`: 189 passed, 2 skipped. This was run before the final suggestion-answer metadata union change; the subsequent V2 suite includes that change.
