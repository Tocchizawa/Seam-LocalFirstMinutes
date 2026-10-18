# Seam v2 UI implementation notes

## Frontend contract

- The live snapshot keeps the existing `topics[]` and `blocks[]` API and adds `document.nodes[]` as the ordered projection. Each node has a stable server `id`, `type` (`topic` or `block`), optional `topic_id`, Markdown, global `position`, and its per-entity `revision`.
- The editor uses those IDs as semantic-unit IDs. Normal edits stay on the existing entity routes with per-entity revisions and idempotency keys. Cross-unit replacements use the narrow atomic `/document/edits` operation endpoint, sending only changed, created, or removed semantic units with the expected document revision; no whole-document Markdown overwrite is sent.
- A server snapshot or live-note WebSocket revision is reconciled by semantic-unit ID. Clean units receive local ProseMirror transactions; a unit with a local draft is left intact and surfaced as a conflict. Other units may still update. `UPDATE_TAKEAWAY` is projected as an ordinary “会議の要点” topic/block node with a stable ID and revision; its block text is editable through the same block route and remains the source of `snapshot.takeaway`.

## Editor behavior

- `LiveNoteDocumentEditor` owns one Tiptap instance for the live note. Each topic and block is an isolated container keyed by its server ID. Paragraph, heading, list, code, link, and table nodes inside a block remain Markdown-backed. Select-all and cross-unit selections are normalized into valid wrappers before dispatch, so ProseMirror cannot invent duplicate server IDs or drop the target block while fitting a replacement.
- Tiptap UniqueID gives document nodes stable editor IDs. Markdown stays the storage/export format; semantic IDs are carried separately and are not serialized into Markdown.
- Human edits are written to per-session localStorage drafts immediately, then debounced to the entity PATCH API. Cross-unit replacements persist their removed IDs and save the changed block plus deletions atomically through `/document/edits`. IME composition pauses saving. Failed or conflicting writes retain the draft; a user can explicitly retry against the newest revision.
- The left outline follows topic units and scrolls to the selected section. The transcript and history remain separate tabs; the note activity/history area stays secondary to the document.

## Assumptions and limits

- The backend projection must provide the exact per-unit revision and Markdown text for each stable node ID. If a projection omits that revision, the UI falls back to the matching legacy topic/block revision.
- A block is the smallest ordinary save and conflict unit. A multi-paragraph edit inside one block is saved as one Markdown string through its existing block route. Cross-unit text replacements preserve text before/after the selection in boundary blocks and save the resulting semantic-unit delta atomically.
- AI changes need to be reflected in `document.nodes` with the same IDs for targeted updates; deleted IDs are removed from the editor on a clean snapshot. The frontend refreshes the snapshot after WebSocket revision events.
- A folded “論点とメモを追加・整理” section keeps the existing creation, deletion, and topic reordering actions available. New-topic drafts and new-block drafts use the existing durable local draft helper; block and topic deletion ask for confirmation.
- The browser spec uses a synthetic local-only API fixture with the same entity and document-delta routes. Chrome checks passed for Markdown rendering, autosave/reload, select-all replacement/reload, remote-update selection/scroll retention, same-unit conflicts with explicit retry, composition deferral, folded topic/block creation, deletion, and topic reordering. Multi-unit select-all emitted one `/document/edits` request containing block update and topic/block deletions. The verifier also checked mobile width with no horizontal overflow and captured light/dark desktop/mobile views.
- Topics remain single heading save units, while paragraphs, headings, lists, code, and tables are supported inside block units. A pasted heading inside a block remains block content; creating a new semantic topic is available through the folded structure controls.

## Changed files

- `gui/src/components/live-notes/LiveNoteDocumentEditor.tsx`
- `gui/src/components/live-notes/LiveNotesPanel.tsx`
- `gui/src/components/live-notes/live-notes.css`
- `gui/tests/live-notes-harness.tsx`
- `gui/tests/live-document.spec.ts`
- `gui/package.json` and `gui/pnpm-lock.yaml` (Tiptap UniqueID and table extensions)
