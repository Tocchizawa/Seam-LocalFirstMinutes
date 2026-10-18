# Seam Live Notes v2 implementation and evaluation report

Status date: 2026-10-18. This report records implementation evidence and known gaps; it does not claim all design acceptance conditions are met.

## Files and responsibilities

**Authoritative target material, unchanged after copy:**

- `docs/SEAM_LIVE_NOTES_V2_DESIGN.md` — v2 product/design target, semantic editing and quality criteria.
- `docs/SEAM_LIVE_NOTES_V2_GOLDEN_SAMPLE.md` — illustrative 5/12/18-minute and final-note examples; not source-verified truth or exact-output assertion.
- `reference/SEAM_LIVE_NOTES_V2_UI_MOCK.html` — visual reference mock.

**Evaluation and repo guidance:**

- `tests/fixtures/live_notes_v2_scenario.json` — synthetic sequential technical transcript with 5-minute unanswered, 12-minute answered, 18-minute return/correction, and 23-minute final checkpoints; includes comparison and outage/recovery variations plus explicit manual edit harness event. No canned provider responses.
- `implementation-notes-v2-docs.md` — actual data/operation/snapshot/API contracts and distinctions among stub, real-provider, and real-audio evidence.
- `docs/live-notes-v2-report.md` — this evidence and limitations report.
- `docs/SEAM_REALTIME_AGENTIC_NOTES_DESIGN.md` — original v1 design annotated with a version pointer; v1 content otherwise retained.
- `docs/architecture.md`, `docs/data-model.md`, `docs/requirements.md`, `docs/live-notes-testing.md`, `README.md` — describe or link current behavior and target v2 evaluation without presenting design goals as measured quality.
- `docs/live-notes-implementation.md` — existing implementation/testing record remains the source for earlier v1 operational history.

## Data, operations, migration, and UI contracts

- SQLite topic/block rows remain canonical. Startup migration adds semantic metadata and first-class questions, batch kind, and finalization state without rewriting saved minutes/transcript. v1 tables and APIs remain for compatibility.
- V2 uses strict schema version 2 with ten operation types: `CREATE_TOPIC`, `UPSERT_KNOWLEDGE`, `RESOLVE_QUESTION`, `REVISE_CLAIM`, `MERGE_BLOCKS`, `MOVE_BLOCK`, `RESTRUCTURE_TOPIC`, `UPDATE_TAKEAWAY`, `SUGGEST_EDIT`, `NO_CHANGE`. V1 plan support remains for compatibility/replay; current CLI uses v2.
- Per-type fields are schema-validated; application validates session membership/evidence, ownership/protection, and expected revisions in a transaction. Applied operation IDs support idempotent replay. Safe question resolution, claim revision and merges preserve evidence/history. Unsafe/protected edits become suggestions; accepting a correction checks target revision, archives prior state, and protects the human-owned replacement.
- Fast, consolidation and finalization jobs share the coordinator semaphore, restrictions, retry policy and call budget. Stop includes pending segments in durable finalization; a separate finalization pass is used when fast processing already finished. Finalization state persists across restart.
- Snapshots retain `topics[].blocks[]` and add ordered `document.nodes[]` and Markdown/revision/questions/takeaway projections. Nodes are not canonical storage. Normal editing uses entity routes; cross-unit changes use atomic `POST /api/live-notes/{session_id}/document/edits` deltas with expected document revision, not a whole-document Markdown patch.
- UI uses one Tiptap editor keyed by stable topic/block IDs, targeted reconciliation, local drafts, IME save deferral, and explicit conflict retry. Browser spec covers render/save/reload, cross-unit replacement, remote update selection/scroll retention, conflict, IME, structural controls, mobile overflow and light/dark views per UI implementation notes.

## Verification evidence and current status

- Backend implementation notes report `py_compile` succeeded. The latest focused V2 backend/real-CLI suite reported 31 passed, 2 skipped; a broader focused suite earlier reported 189 passed, 2 skipped before the final suggestion-answer metadata union change.
- Final full Python gate: `.venv/bin/pytest -q` reported **274 passed, 7 skipped in 57.76 seconds**. The legacy custom script assertions reported **207 passed, 0 failed**. An earlier run had two lifecycle failures; fixes addressed digest SQL, stopped final-batch cursor handling, pending-consolidation visibility, and finalize cursor completion. The sandbox emitted a non-fatal no-Metal-device warning at exit; no real Whisper inference is implied.
- Playwright’s latest full result is **22 passed, 4 failed (26 total)**. The new `live-document.spec.ts` suite is **3 passed, 1 failed**. Offline manual-draft recovery now passes. The four remaining failures are: splitting a block on Enter produces one paragraph where two are expected; focus is lost after a remote AI update; focus is lost after a remote delete plus update; and the same-block conflict path never issues the expected PATCH/409. GUI `tsc`/build and OSS preflight passed; Vite reports a large chunk warning.
- Automated tests and browser fixtures are synthetic/local. The OS IME path was tested synthetically, not with a native macOS IME session.
- Claude binary is absent (`shutil.which("claude")` returned null); no install/login was attempted. Claude real-provider quality is unmeasured. Codex CLI preflight was run.

## Real-model quality evidence

Provider/model was Codex with the saved configuration `gpt-6-luna`, low reasoning. Seven authorized calls were used in total: four initial calls (one malformed 5-minute response, followed by three successful old-prompt calls) and three calls in the revised-prompt progression. The original four-call result produced a long paragraph and missed a secondary topic. This is the captured baseline failure.

In the revised progression, the 5-minute response was accepted in 11.010 seconds. The 12-minute response arrived in 14.257 seconds but initially failed runtime validation because it included `canonical_terms` on `RESOLVE_QUESTION` and `UPDATE_TAKEAWAY`. After backend schema/runtime support was fixed, the captured raw JSON content was replayed without another model call; batch/op IDs, base revision, and physical target IDs were rebased to the fresh database. The replay was not byte-for-byte identical to the original envelope. The verifier recorded the ID/revision mappings and `raw_outputs_edited=false`; provider_calls was zero. It resolved the NRI reachability question and added canonical terms. This response is available as a replay regression, not a new inference or proof that all provider calls will pass.

The actual 23-minute provider response arrived in 10.640 seconds but referenced plan-local topic `new:1` with `UPSERT_KNOWLEDGE` before its `CREATE_TOPIC`. At that point, application rejected it with `TOPIC_NOT_FOUND` and rolled back the transaction. A backend fix now resolves plan-local topic references before applying operations; the captured final response content was replayed without another model call after rebasing batch/op IDs, base revision, and physical target IDs to the fresh database. This was not a byte-for-byte replay of the original envelope. The verifier recorded the ID/revision mappings and `raw_outputs_edited=false`; provider_calls was zero. The replay output contained a separate `設定ファイルの保存場所` topic/fact and a `会議の要点` takeaway combining the NRI path, proposed-but-uncommitted connection direction, code-verification and deadline uncertainty, and the config.yaml location.

The replayed final state had three topics (NRI, meeting takeaway, and settings), cursor 15, and no pending segments. One question was answered and the NRI/NewGRPCServer argument-use question remained open; redundant code-path and conclusion blocks persisted, and the note did not have the expected subsection headings. Finalization completion was fixed and the final replay state reports `finalize_pass_done=true`, without another CLI call. This evidence shows the secondary topic was represented after the fix/replay, but does not prove complete question resolution or the intended document structure. Q02 is partial; Q09 and Q11 remain unproven. No rubric score is claimed.

No inference calls remain from the authorized maximum. No real Claude CLI run or real audio/device run is claimed. Synthetic transcript replay does not establish ASR accuracy or real meeting behavior.

## Q01–Q15 evidence status

The design’s Q01–Q15 acceptance list remains the authoritative quality target. Evidence below is limited to current deterministic contract/UI runs and captured real-model outputs; it is not a declaration of full DoD completion.

| ID | Current evidence/status |
|---|---|
| Q01 repeated concepts do not create duplicate headings | Semantic operations and topic reuse are implemented; final scenario output was not scored comprehensively. Partial. |
| Q02 answered question leaves open list | 12-minute response content replay, with run metadata rebased to a fresh DB, answered the reachability question, but the final state still had one `NewGRPCServer` argument-use question open, with redundant code-path/conclusion blocks. Partial / not passed as a whole scenario. |
| Q03 corrections supersede old claim | Backend unit contract covers claim revision/archive; no complete real-provider correction-quality pass. Not proven end to end. |
| Q04 topic return reuses same ID | Replay/operation contract supports existing topic targeting; no final scored checkpoint evidence. Partial. |
| Q05 merge retains evidence | Repository contract and deterministic backend tests cover safe merges/evidence union. This is contract evidence, not model merge quality. |
| Q06 human edits remain protected | API/repository contract plus UI conflict/draft tests cover protection paths. Native OS IME remains untested. |
| Q07 retries/WS reconnect do not duplicate | Idempotency and integration tests cover deterministic replay; full result depends on final Python suite rerun. |
| Q08 uncertain technical terms are not asserted | `verified_against_source=true` is rejected from transcript-only evidence; semantic reviewer pass is incomplete. |
| Q09 technical explanation structure | The actual response content replay added a separate config topic and takeaway, but retained redundant code-path/conclusion blocks and lacked expected subsection headings. Not passed. |
| Q10 comparison structure | A comparison fixture exists; no reported real-provider comparison run. Not evaluated. |
| Q11 checkpoint documents improve coherently | Baseline was a long paragraph missing a secondary topic; revised run/replay improved topic presence but retains an open question and lacks subsection structure. Not passed. |
| Q12 reading/editing does not jump | Playwright reports focus loss after remote AI update and remote delete+update. Offline draft recovery passes. Same-block conflict coverage also fails because the expected PATCH/409 is never issued. Partial. |
| Q13 resolved questions do not accumulate | The 12-minute question resolution replay is positive, but one related question remains open in final state. Partial. |
| Q14 slow CLI does not block recording/manual edits | Python suite passes; delay/outage and browser cases are synthetic. No real long-meeting run. Contract evidence only. |
| Q15 history/source/Undo work | Backend contracts and full Python suite cover event/evidence/undo paths; no real audio run. Contract evidence only. |

## Before/after record for future runs

```text
Run date:
Baseline revision / prompt:
Candidate revision / prompt:
Scenario and checkpoints:
Provider/model and reasoning setting:
Calls attempted / completed / malformed:
Authorization source and scope:

Observed output at 5m (still unanswered?):
Observed output at 12m (answer resolved and evidence retained?):
Observed output at 18m (prior topic reused and correction protected?):
Observed final output at 23m (organized, faithful, no resolved open item?):
Comparison variation:
Outage/recovery variation:

Stub contract results (separate from semantic review):
Real-provider semantic scores (0-5, only after review): organization / consolidation / state accuracy / faithfulness / readability / continuity:
Evidence for each score:
Unrun checks and limitations:
```

## Known limits and pending evidence

- Full Python suite is green at 274 passed / 7 skipped, and legacy custom script assertions are 207 passed / 0 failed.
- Latest Playwright is 22/26. The document-editor spec is 3/4. Offline draft recovery passes; the four remaining failures are block split paragraph count, focus after remote AI update, focus after remote delete+update, and same-block conflict PATCH/409 never issued.
- The model results still leave one technical question open and did not produce the intended subsection structure. Semantic quality DoD is therefore not met on current evidence.
- Claude CLI is absent in this environment; no install or login was attempted. Real Claude inference is unmeasured.
- OS-native IME behavior, real microphone/ScreenCaptureKit/Whisper, and actual long meetings are not covered by synthetic API/browser fixtures.
- The golden sample's code-specific terminology is illustrative until verified against the source project discussed by the synthetic meeting.
- Passing schema, persistence, replay, and UI tests establish implementation contracts within those fixtures, not general LLM quality. The final response’s successful replay does not change the original call’s captured `TOPIC_NOT_FOUND` failure. Provider behavior may vary across model versions and inputs.
