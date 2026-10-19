# Suggestion review UI notes

- The note stays the primary content. Pending review items are summarized by a collapsed `確認が必要 N件` disclosure before the editor.
- Review content accepts the optional `presentation` object and safely parses legacy `suggested_operation_json` when that object is absent. Unparseable or incomplete legacy data falls back to plain Japanese text rather than exposing JSON or internal identifiers.
- Current Markdown is taken from presentation data first and then matched against the current topic or block in the snapshot. Source references render available sequence numbers, `start_sec` timestamps, and transcript text; old block source sequence numbers are used when presentation metadata is absent.
- Unknown review reasons stay neutral. Explicit backend reason text is shown as supplied; known reason codes have Japanese copy for a hand edit, a newer AI update, or a closed question.
- Successful actions announce the result and remove the item from the pending count. A 409 tells the user that the note changed and keeps their editor content in place; other failures show the API error.
- The browser fixture uses only synthetic local data and a mocked local API. It covers legacy JSON parsing, collapsed/readable review, keep-current action, and stale-action draft retention.

## Assumptions

- `presentation.source_segments` uses objects with optional `seq`, `start_sec`, and `text` fields, as confirmed by the backend lane.
- A legacy operation can be summarized only when it contains a recognizable operation and proposal value. Otherwise the UI uses a neutral prompt and keeps the raw operation private.
- Successfully resolved suggestions should leave the pending count immediately and announce the action result.

## Deviations

- The existing `paragraph splitting and cross-section select-all edits stay attached to semantic units` Playwright case remains failing. After its click, `End`, `Enter`, and typing sequence, the target block retains one direct paragraph; browser inspection showed the inserted `追記した段落` paragraph has no `[data-semantic-unit-id]` ancestor. That orphaned editor content is outside this suggestion UI change and needs a separate editor fix.
