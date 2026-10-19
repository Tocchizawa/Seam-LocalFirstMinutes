# Live Notes v3 backend implementation notes

## Contract

- The existing SQLite topic/block model, v1 manual APIs, and v1/v2 persisted operation batches remain supported. Current model-facing structured output is schema version 3 with `MERGE_TOPICS` and `SET_ITEM_STATUS` added. V3 also accepts bounded topic digests and revision-checked `supports`, `contradicts`, and `related` proposals between stable same-session items.
- The existing snapshots retain their keys and add `document.title_revision`, `document.takeaway_block_id`, `document.unresolved_count`, and `document.needs_verification_count`. Questions expose independent `conversation_state` and `verification_state` values. Existing `state` remains for compatibility. Topic digests feed outline/retrieval, with block-text fallback only for empty legacy digests.
- `/document/edits` accepts revision-checked `update_document_title` and `update_document_takeaway` operations. `/control` accepts `consolidate` for an explicit structure pass.

## Persistence and safety

- Migration v3 adds canonical keys/digests, item status, separate question states, consolidation reasons, item relations, rejected/deleted intent keys, and an FTS5 trigram search index when supported. The fallback uses normalized token overlap. Existing note and transcript text is not rewritten by migration.
- Migration additions run behind a savepoint and can be rolled back together. Question rows migrate `answered` to `answered_in_discussion`; verification starts `unverified` only during the one-time pre-v3 backfill. Reopening a v3 database preserves later question state.
- Fast batches cannot merge or restructure topics. Topic merge is revision-checked, preserves IDs through an archived source topic and relation, and is limited to consolidate/finalize. Human-owned topic titles conflict into suggestions.
- A discussion answer never marks external verification. Question status operations check both the question revision and its question block revision. Claim, decision, and action item statuses are role-specific; `verified_against_source` cannot be supplied as a free item status.
- `SET_ITEM_STATUS` cannot mark a question answered; answering requires `RESOLVE_QUESTION` with answer text and evidence. External verification is not yet an editable transition because this local meeting operation has no attestation source; only `unverified` and `needs_verification` are writable states.
- Existing answer block updates require the answer block's own `expected_revisions` entry in schema v3. Answers, superseded blocks, and merged duplicates receive durable relation rows; model-declared relations verify same-session target ownership and expected target revision. Relations participate in event undo checks.
- Human-deleted and dismissed text creates a normalized intent tombstone. Identical future semantic upserts are suppressed.
- The document context remains bounded to 36k block-content characters with a global candidate cap of 48 blocks across all topics. Open questions are supplied independently of topic ranking. A single host-side context expansion may retrieve explicitly requested existing IDs after truncation or for a supplied open question; structural repair and expansion cannot both run in the same batch.
- Consolidation uses the existing 240-second cooldown, four-fast-batch minimum, and unchanged-document digest check. Explicit manual consolidation coalesces with an active pass and can run without newly ingested segments.
- Automatic consolidation additionally requires a persisted semantic trigger (`duplicates`, `resolved_old_text`, `topic_departure`, `topic_switch`, or `density`) recorded on completed fast batches. It waits for pending speech and does not schedule interval-only no-change passes; explicit manual consolidation remains available. Questions cannot be marked answered through `SET_ITEM_STATUS`; `RESOLVE_QUESTION` is required to provide answer text, evidence, and the answers relation. Since there is no external-source attestation flow, status operations cannot set `verified_against_source`.
- Host-side context expansion includes the requested older speech text in the bounded provider context, so the second inference can cite and validate its source IDs. Every schema-v3 document change requires a cited segment even in consolidation and final review; `NO_CHANGE` is exempt.

## Assumptions and deviations

- The semantic question states are projected alongside the legacy `state` field rather than replacing it, because existing clients and replay batches use `state`.
- Topic merges archive the source topic while moving its active blocks to the target. The topic relation table is also used to retain the duplicate-topic pointer; block/item relations retain the full declared relation vocabulary.
- Rejected/deleted intent matching uses a normalized canonical key, not semantic embedding similarity. The key includes text, aliases, and canonical terms; materially different wording may still require future semantic review.
- The FTS5 index is rebuilt for the target session during context retrieval to ensure legacy and human-edited text is current. It does not create a separate external vector database.

## Validation

Focused validation (synthetic only; no real-session database or provider inference):

- `.venv/bin/python -m py_compile src/live_notes/models.py src/live_notes/repository.py src/live_notes/coordinator.py src/api/live_notes.py` passed.
- `.venv/bin/python -m pytest tests/test_live_notes_v3_backend.py tests/test_live_notes_v2_backend.py -q` passed (45 tests).
- `.venv/bin/pytest -q tests/test_live_notes_scheduler.py` passed (10 tests), including the persistent call-budget and single-repair checks.
- Old migration-marker assertions expect v3 while v1/v2 replay/provider contract tests still parse schema v2. Tests assert automatic consolidation requires persisted semantic triggers and waits for pending speech.
- The v3 tests cover migration idempotency/reopen state, digest persistence and retrieval, revision-separated question status, relation revision checks and undo, explicit consolidation, context expansion, and intent tombstones.
