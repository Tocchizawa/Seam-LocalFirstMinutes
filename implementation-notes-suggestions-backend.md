# Suggestion lifecycle backend changes

## Behavior

- `RESOLVE_QUESTION.expected_revision` applies only to the question row. An existing answer block has its own revision in `expected_revisions[block_id]`. Older plans may omit that map entry; the store then reads the answer revision from the captured `related_topics` context when available. It never compares a question revision with a block revision.
- Repeating an answered question with identical normalized wording is idempotent. Newly cited segments are unioned into the answer and question evidence. A materially new answer updates the same AI-owned answer when its current revision matches the captured context or explicit answer revision. The prior wording remains as a superseded, evidence-bearing block.
- A materially new answer against a human-protected answer becomes a `SUGGEST_EDIT` on that answer. A novel answer against a stale AI answer also becomes a revision-checked `SUGGEST_EDIT` on the current answer, retaining its transcript evidence. Acceptance revalidates the target revision; stale suggestions are superseded and return the existing 409 response without changing content.
- A stale resolution against a question that remains open fails the batch with a revision conflict. The transaction rolls back, so the evidence cursor does not advance and the coordinator can retry within its existing retry budget. A deferred question can retain a new answer candidate as a topic-targeted suggestion.
- Pending suggestions are deduplicated per target and operation intent. Identical candidates reuse the existing row and union evidence; revised wording supersedes the older candidate. Superseded and dismissed rows remain in history and are omitted from snapshots. Startup migration marks legacy pending `RESOLVE_QUESTION` suggestions superseded when their question is already closed.
- Accepting a suggestion on an AI-owned block preserves AI ownership and does not set `user_touched` or `protected`. An already human-protected block keeps its existing protection. Actual manual edits continue to set those fields.
- Snapshot suggestions preserve legacy fields and add `presentation`: `summary`, `reason_code`, `target_title`, `current_md`, `proposed_md`, and `source_segments`. The reason code uses the UI keys `user_edit`, `newer_ai_update`, or `closed_question`. Topic targets show the current title for title proposals and rendered topic Markdown for content proposals. Summaries name the proposed action; proposed text remains in `proposed_md`. Source references use `{seq, start_sec, text}`.

## Assumptions

- The captured related-topic context is sufficient to verify an answer revision when older plans omit the dedicated answer revision map.
- A materially new resolution is safe to apply directly only when the target is still the same current AI answer. A human-protected answer always requires review.
- Stale AI answer wording is meaningful evidence. Keeping it as a pending, current-revision proposal preserves the candidate and its source segments without another inference call.

## Deviations

- For stale novel AI answers, the backend uses a conservative review suggestion against the current answer instead of automatically re-evaluating the candidate during the same batch. This preserves the wording and evidence, avoids an unbounded inference call, and prevents stale text from overwriting a newer AI revision.
- A stale open-question batch is retried through the coordinator's existing bounded retry policy rather than advancing its evidence cursor. This can fail after the configured retry limit, but avoids silently discarding the answer candidate.
- No schema migration or inference call was added. The startup repair changes only the status of impossible legacy pending suggestions; history remains intact.
