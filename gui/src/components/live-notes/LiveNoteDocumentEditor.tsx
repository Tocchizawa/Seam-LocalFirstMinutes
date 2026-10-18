import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import { Node as TiptapNode } from "@tiptap/core";
import { DOMParser as PMDOMParser, type Node as PMNode } from "@tiptap/pm/model";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import TaskList from "@tiptap/extension-task-list";
import TaskItem from "@tiptap/extension-task-item";
import { Markdown } from "tiptap-markdown";
import { TableKit } from "@tiptap/extension-table";
import UniqueID from "@tiptap/extension-unique-id";
import { AllSelection, TextSelection } from "@tiptap/pm/state";
import { canSplit } from "@tiptap/pm/transform";
import { getLiveSuggestionReview, LiveApiError, newRequestId, useNewNoteDraft, type LiveNotesController, type LiveSnapshot, type LiveTopic } from "../../lib/live-notes";

type SemanticUnit = { id: string; type: "document_title" | "document_takeaway" | "topic" | "block"; topic_id?: string; markdown: string; position: number; revision: number; kind?: string; semantic_role?: string; epistemic_state?: string; resolution_state?: string; user_touched?: boolean; protected?: boolean; source_segments?: number[] };
type UnitDraft = { markdown: string; baseRevision: number; requestId: string };
const TopicUnitNode = TiptapNode.create({
  name: "topicUnit", group: "block", content: "heading", defining: true, isolating: true,
  addAttributes: () => ({ semanticUnitId: { default: null, parseHTML: (element: HTMLElement) => element.dataset.semanticUnitId ?? null, renderHTML: (attrs: Record<string, unknown>) => ({ "data-semantic-unit-id": attrs.semanticUnitId }) } }),
  parseHTML: () => [{ tag: "section[data-semantic-unit-id][data-unit-type=topic]", getAttrs: (element) => ({ semanticUnitId: (element as HTMLElement).dataset.semanticUnitId }) }],
  renderHTML: ({ HTMLAttributes }) => ["section", { ...HTMLAttributes, "data-unit-type": "topic" }, 0],
});
const BlockUnitNode = TiptapNode.create({
  name: "blockUnit", group: "block", content: "block+", defining: true, isolating: true,
  addAttributes: () => ({ semanticUnitId: { default: null, parseHTML: (element: HTMLElement) => element.dataset.semanticUnitId ?? null, renderHTML: (attrs: Record<string, unknown>) => ({ "data-semantic-unit-id": attrs.semanticUnitId }) } }),
  parseHTML: () => [{ tag: "section[data-semantic-unit-id][data-unit-type=block]", getAttrs: (element) => ({ semanticUnitId: (element as HTMLElement).dataset.semanticUnitId }) }],
  renderHTML: ({ HTMLAttributes }) => ["section", { ...HTMLAttributes, "data-unit-type": "block" }, 0],
});
const DocumentTitleNode = TiptapNode.create({
  name: "documentTitleUnit", group: "block", content: "heading", defining: true, isolating: true,
  addAttributes: () => ({ semanticUnitId: { default: null, parseHTML: (element: HTMLElement) => element.dataset.semanticUnitId ?? null, renderHTML: (attrs: Record<string, unknown>) => ({ "data-semantic-unit-id": attrs.semanticUnitId }) } }),
  parseHTML: () => [{ tag: "section[data-semantic-unit-id][data-unit-type=document-title]", getAttrs: (element) => ({ semanticUnitId: (element as HTMLElement).dataset.semanticUnitId }) }],
  renderHTML: ({ HTMLAttributes }) => ["section", { ...HTMLAttributes, "data-unit-type": "document-title" }, 0],
});
const DocumentTakeawayNode = TiptapNode.create({
  name: "documentTakeawayUnit", group: "block", content: "block+", defining: true, isolating: true,
  addAttributes: () => ({ semanticUnitId: { default: null, parseHTML: (element: HTMLElement) => element.dataset.semanticUnitId ?? null, renderHTML: (attrs: Record<string, unknown>) => ({ "data-semantic-unit-id": attrs.semanticUnitId }) } }),
  parseHTML: () => [{ tag: "section[data-semantic-unit-id][data-unit-type=document-takeaway]", getAttrs: (element) => ({ semanticUnitId: (element as HTMLElement).dataset.semanticUnitId }) }],
  renderHTML: ({ HTMLAttributes }) => ["section", { ...HTMLAttributes, "data-unit-type": "document-takeaway" }, 0],
});

function readUnits(snapshot: LiveSnapshot): SemanticUnit[] {
  const document = snapshot.document as (LiveSnapshot["document"] & { nodes?: SemanticUnit[] }) | undefined;
  const documentUnits = document?.nodes;
  const title: SemanticUnit = { id: "document-title", type: "document_title", markdown: `# ${document?.title || "議論ノート"}`, position: -2000, revision: Number(document?.title_revision ?? 0) };
  const isLegacyTakeawayHeading = (topicId: string) => {
    const topic = snapshot.topics.find((item) => item.id === topicId);
    const onlyBlock = topic?.blocks.length === 1 ? topic.blocks[0] : null;
    if (!document?.takeaway || topic?.title_owner !== "ai" || !onlyBlock) return false;
    if (document.takeaway_block_id) return onlyBlock.id === document.takeaway_block_id;
    // Pre-v3 metadata has no takeaway ID. Its dedicated AI context heading may
    // outlive a later manual edit to the synthetic document takeaway.
    return onlyBlock.content_md.trim() === document.takeaway.trim()
      || (onlyBlock.kind === "context" && /^(会議の要点|要点|概要|summary)$/i.test(topic.title.trim()));
  };
  if (documentUnits?.length) {
    const nodes: SemanticUnit[] = documentUnits.filter((unit) => !(unit.type === "topic" && isLegacyTakeawayHeading(unit.id))).map((unit) => {
    const topic = snapshot.topics.find((item) => item.id === unit.id);
    const block = snapshot.topics.flatMap((item) => item.blocks).find((item) => item.id === unit.id);
    return { ...unit, revision: Number((unit as SemanticUnit & { revision?: number }).revision ?? topic?.revision ?? block?.revision ?? snapshot.note_revision), kind: unit.kind ?? block?.kind } as SemanticUnit;
    });
    const hasTakeawayBlock = document?.takeaway && (document.takeaway_block_id
      ? nodes.some((unit) => unit.id === document.takeaway_block_id)
      : nodes.some((unit) => unit.type === "block" && unit.markdown.trim() === document.takeaway?.trim()));
    nodes.unshift(title);
    if (document?.takeaway && !hasTakeawayBlock) nodes.splice(1, 0, { id: "document-takeaway", type: "document_takeaway", markdown: `## 要点\n\n${document.takeaway}`, position: -1000, revision: Number(document.takeaway_revision ?? snapshot.note_revision) });
    return nodes;
  }
  const units: SemanticUnit[] = [title];
  if (document?.takeaway) units.push({ id: "document-takeaway", type: "document_takeaway", markdown: `## 要点\n\n${document.takeaway}`, position: -1000, revision: Number(document.takeaway_revision ?? snapshot.note_revision) });
  for (const topic of [...snapshot.topics].sort((a, b) => a.position - b.position)) {
    if (isLegacyTakeawayHeading(topic.id)) continue;
    units.push({ id: topic.id, type: "topic", markdown: `## ${topic.title}`, position: topic.position * 1000, revision: topic.revision });
    for (const block of [...topic.blocks].sort((a, b) => a.position - b.position)) units.push({ id: block.id, type: "block", topic_id: topic.id, markdown: block.content_md, position: topic.position * 1000 + block.position + 1, revision: block.revision, kind: block.kind });
  }
  return units;
}
function md(editor: NonNullable<ReturnType<typeof useEditor>>, node: PMNode) {
  const storage = editor.storage as { markdown?: { serializer?: { serialize: (node: PMNode) => string } } };
  return storage.markdown?.serializer?.serialize(node) ?? node.textContent;
}
function nodesForMarkdown(editor: NonNullable<ReturnType<typeof useEditor>>, unit: SemanticUnit): PMNode[] {
  const storage = editor.storage as { markdown?: { parser?: { parse: (text: string) => string } } };
  const host = document.createElement("div");
  host.innerHTML = storage.markdown?.parser?.parse(unit.markdown) ?? unit.markdown;
  const fragment = PMDOMParser.fromSchema(editor.schema).parse(host);
  const type = unit.type === "topic" ? editor.schema.nodes.topicUnit : unit.type === "document_title" ? editor.schema.nodes.documentTitleUnit : unit.type === "document_takeaway" ? editor.schema.nodes.documentTakeawayUnit : editor.schema.nodes.blockUnit;
  return [type.create({ semanticUnitId: unit.id }, fragment.content)];
}
function unitMarkdown(editor: NonNullable<ReturnType<typeof useEditor>>, nodes: PMNode[]) {
  return md(editor, editor.schema.node("doc", null, nodes.flatMap((node) => node.content.content)));
}
function ranges(editor: NonNullable<ReturnType<typeof useEditor>>) {
  const found = new Map<string, { from: number; to: number; nodes: PMNode[] }>();
  editor.state.doc.forEach((node, offset) => {
    const start = offset;
    const id = node.attrs.semanticUnitId as string | null;
    if (id) {
      const prior = found.get(id);
      found.set(id, { from: prior?.from ?? start, to: start + node.nodeSize, nodes: [...(prior?.nodes ?? []), node] });
    }
  });
  return found;
}
function draftKey(sessionId: string) { return `seam:live-document-draft:v1:${sessionId}`; }
function loadDraft(sessionId: string): Record<string, UnitDraft> {
  try { return JSON.parse(localStorage.getItem(draftKey(sessionId)) || "{}") as Record<string, UnitDraft>; } catch { return {}; }
}
function structuralDraftKey(sessionId: string) { return `seam:live-document-deletes:v1:${sessionId}`; }
function loadStructuralDeletes(sessionId: string) {
  try { return new Set(JSON.parse(localStorage.getItem(structuralDraftKey(sessionId)) || "[]") as string[]); } catch { return new Set<string>(); }
}
function isDocumentWideSelection(view: import("@tiptap/pm/view").EditorView) {
  if (view.state.selection instanceof AllSelection) return true;
  const native = window.getSelection();
  if (!native || native.isCollapsed || !native.anchorNode || !native.focusNode || !view.dom.contains(native.anchorNode) || !view.dom.contains(native.focusNode)) return false;
  const selected = native.toString();
  const text = view.dom.textContent ?? "";
  return selected.length > 0 && selected === text;
}
function selectedUnitIds() {
  const native = window.getSelection();
  if (!native?.anchorNode || !native.focusNode) return null;
  const unitId = (node: Node) => (node instanceof Element ? node : node.parentElement)?.closest<HTMLElement>("[data-semantic-unit-id]")?.dataset.semanticUnitId ?? null;
  const anchor = unitId(native.anchorNode), focus = unitId(native.focusNode);
  return anchor && focus && anchor !== focus ? { anchor, focus } : null;
}

export function LiveNoteDocumentEditor({ controller, snapshot, onError }: { controller: LiveNotesController; snapshot: LiveSnapshot; onError: (message: string | null) => void }) {
  const units = useMemo(() => readUnits(snapshot).sort((a, b) => a.position - b.position), [snapshot]);
  const unitRef = useRef(units); unitRef.current = units;
  const draftRef = useRef<Record<string, UnitDraft>>(loadDraft(snapshot.session_id));
  const structuralDeletes = useRef(loadStructuralDeletes(snapshot.session_id));
  const [draftVersion, setDraftVersion] = useState(0);
  const [saveState, setSaveState] = useState("保存済み");
  const [conflicted, setConflicted] = useState<string[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [editingUnitId, setEditingUnitId] = useState<string | null>(null);
  const editorFocused = useRef(false);
  const [suggestionBusy, setSuggestionBusy] = useState<string | null>(null);
  const [resolvedSuggestions, setResolvedSuggestions] = useState<Record<string, "apply" | "keep">>({});
  const [suggestionFeedback, setSuggestionFeedback] = useState("");
  const topicDraft = useNewNoteDraft(`seam:live-new-topic:v1:${snapshot.session_id}`);
  const timer = useRef<number | null>(null);
  const composing = useRef(false);
  const composingDocumentReplacement = useRef(false);
  const inFlight = useRef(false);
  const saveRef = useRef<(force?: boolean) => Promise<void>>(async () => {});
  const replaceWholeDocument = (view: import("@tiptap/pm/view").EditorView, markdown: string) => {
    if (!editor || !view.state.selection.empty && (view.state.selection.from > 0 || view.state.selection.to < view.state.doc.content.size)) return false;
    const serverUnits = unitRef.current;
    const topic = serverUnits.find((unit) => unit.type === "topic");
    const block = serverUnits.find((unit) => unit.type === "block" && unit.topic_id === topic?.id);
    if (!topic || !block) return false;
    const current = ranges(editor);
    const title = serverUnits.find((unit) => unit.type === "document_title");
    const takeaway = serverUnits.find((unit) => unit.type === "document_takeaway");
    const topicNode = current.get(topic.id)?.nodes[0] ?? nodesForMarkdown(editor, topic)[0];
    const blockNode = nodesForMarkdown(editor, { ...block, markdown })[0];
    const preserved = new Set([topic.id, block.id, title?.id, takeaway?.id].filter(Boolean));
    for (const unit of serverUnits) if (!preserved.has(unit.id)) {
      structuralDeletes.current.add(unit.id);
      delete draftRef.current[unit.id];
    }
    persistDrafts();
    try { localStorage.setItem(structuralDraftKey(snapshot.session_id), JSON.stringify([...structuralDeletes.current])); }
    catch { setSaveState("端末への構成下書き保存に失敗しました"); }
    const titleNode = title ? current.get(title.id)?.nodes[0] ?? nodesForMarkdown(editor, title)[0] : null;
    const takeawayNode = takeaway ? current.get(takeaway.id)?.nodes[0] ?? nodesForMarkdown(editor, takeaway)[0] : null;
    const tr = view.state.tr.replaceWith(0, view.state.doc.content.size, [titleNode, takeawayNode, topicNode, blockNode].filter((node): node is PMNode => Boolean(node)));
    tr.setSelection(TextSelection.near(tr.doc.resolve(Math.max(1, tr.doc.content.size - 1)), -1));
    view.dispatch(tr.setMeta("seamWholeReplacement", true));
    capture(editor);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { if (!composing.current) void saveRef.current(false); }, 650);
    return true;
  };
  const replaceCrossUnitSelection = (view: import("@tiptap/pm/view").EditorView, markdown: string) => {
    if (!editor) return false;
    const selected = selectedUnitIds();
    if (!selected) return false;
    let from: number, to: number;
    try {
      const anchor = view.posAtDOM(window.getSelection()!.anchorNode!, window.getSelection()!.anchorOffset);
      const focus = view.posAtDOM(window.getSelection()!.focusNode!, window.getSelection()!.focusOffset);
      from = Math.min(anchor, focus); to = Math.max(anchor, focus);
    } catch { return false; }
    const map = ranges(editor);
    const start = map.get(selected.anchor), end = map.get(selected.focus);
    if (!start || !end || start.from === end.from) return false;
    const touched = [...map].filter(([, range]) => range.from < to && range.to > from);
    const touchedBlockIds = touched.filter(([id]) => unitRef.current.find((unit) => unit.id === id)?.type === "block").map(([id]) => id);
    const targetId = touchedBlockIds[0];
    const targetRange = targetId ? map.get(targetId) : null;
    const target = targetId ? unitRef.current.find((unit) => unit.id === targetId) : null;
    if (!target || !targetRange) return false;
    const firstTouchedBlockRange = map.get(touchedBlockIds[0]!);
    const lastTouchedBlockRange = map.get(touchedBlockIds[touchedBlockIds.length - 1]!);
    const prefix = from > firstTouchedBlockRange!.from + 1 ? view.state.doc.slice(firstTouchedBlockRange!.from + 1, Math.min(from, firstTouchedBlockRange!.to - 1)).content.content : [];
    const suffix = to < lastTouchedBlockRange!.to - 1 ? view.state.doc.slice(Math.max(to, lastTouchedBlockRange!.from + 1), lastTouchedBlockRange!.to - 1).content.content : [];
    const inserted = nodesForMarkdown(editor, { ...target, markdown })[0].content.content;
    const replacement = editor.schema.nodes.blockUnit.create({ semanticUnitId: targetId }, [...prefix, ...inserted, ...suffix]);
    const removed = new Set(touchedBlockIds.filter((id) => id !== targetId));
    for (const id of removed) {
      structuralDeletes.current.add(id);
      delete draftRef.current[id];
    }
    if (removed.size) {
      persistDrafts();
      try { localStorage.setItem(structuralDraftKey(snapshot.session_id), JSON.stringify([...structuralDeletes.current])); }
      catch { setSaveState("端末への構成下書き保存に失敗しました"); }
    }
    const output: PMNode[] = [];
    editor.state.doc.forEach((node) => {
      const id = node.attrs.semanticUnitId as string | null;
      if (id === targetId) output.push(replacement);
      else if (!id || !removed.has(id)) output.push(node);
    });
    const tr = view.state.tr.replaceWith(0, view.state.doc.content.size, output);
    const targetIndex = output.findIndex((node) => node.attrs.semanticUnitId === targetId);
    const targetStart = output.slice(0, targetIndex).reduce((size, node) => size + node.nodeSize, 0);
    const targetEnd = targetStart + replacement.nodeSize - 1;
    tr.setSelection(TextSelection.near(tr.doc.resolve(Math.max(targetStart + 2, targetEnd - 1)), -1));
    view.dispatch(tr.setMeta("seamCrossUnitReplacement", true));
    capture(editor);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { if (!composing.current) void saveRef.current(false); }, 650);
    return true;
  };
  const replaceUserSelection = (view: import("@tiptap/pm/view").EditorView, markdown: string) =>
    isDocumentWideSelection(view) ? replaceWholeDocument(view, markdown) : replaceCrossUnitSelection(view, markdown);
  const editor = useEditor({
    extensions: [StarterKit.configure({ heading: { levels: [1, 2, 3, 4] }, link: false }), Link.configure({ openOnClick: false, linkOnPaste: true, HTMLAttributes: { rel: "noopener noreferrer", target: "_blank" } }), TaskList, TaskItem.configure({ nested: true }), TableKit.configure({ table: { resizable: false } }), TopicUnitNode, BlockUnitNode, DocumentTitleNode, DocumentTakeawayNode, UniqueID.configure({ attributeName: "seamNodeUid", types: ["topicUnit", "blockUnit", "documentTitleUnit", "documentTakeawayUnit", "paragraph", "heading", "bulletList", "orderedList", "blockquote", "codeBlock", "horizontalRule", "taskList", "table"] }), Markdown.configure({ html: false, linkify: true, transformPastedText: true, breaks: false })],
    content: "",
    onUpdate: ({ editor: current, transaction }) => { if (transaction.getMeta("seamRemote")) return; capture(current); },
    editorProps: {
      attributes: { class: "seam-document-editor md-body", role: "textbox", "aria-label": "議論ノート", "aria-multiline": "true" },
      handleKeyDown: (view, event) => {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a") {
          view.dispatch(view.state.tr.setSelection(new AllSelection(view.state.doc)));
          return true;
        }
        if (event.key === "Enter" && !event.shiftKey && !composing.current && view.state.selection.empty) {
          const { $from } = view.state.selection;
          let semanticContainer: PMNode | null = null;
          for (let depth = $from.depth; depth >= 0; depth--) {
            const node = $from.node(depth);
            if (["documentTitleUnit", "blockUnit", "documentTakeawayUnit"].includes(node.type.name)) { semanticContainer = node; break; }
          }
          if (semanticContainer?.type.name === "documentTitleUnit") return true;
          if ((semanticContainer?.type.name === "blockUnit" || semanticContainer?.type.name === "documentTakeawayUnit") && $from.parent.isTextblock) {
            if (canSplit(view.state.doc, $from.pos, 1)) {
              view.dispatch(view.state.tr.split($from.pos));
              return true;
            }
          }
        }
        if ((event.key === "Backspace" || event.key === "Delete") && (isDocumentWideSelection(view) || selectedUnitIds())) return replaceUserSelection(view, "");
        return false;
      },
      handleTextInput: (view, _from, _to, text) => (isDocumentWideSelection(view) || selectedUnitIds()) ? replaceUserSelection(view, text) : false,
      handlePaste: (view, event) => {
        if (!isDocumentWideSelection(view) && !selectedUnitIds()) return false;
        const text = event.clipboardData?.getData("text/plain");
        return text == null ? false : replaceUserSelection(view, text);
      },
      handleDOMEvents: {
        focus: () => { editorFocused.current = true; return false; },
        blur: (view, event) => {
          const relatedTarget = (event as FocusEvent).relatedTarget;
          // ProseMirror may briefly move focus to <body> while reconciling a
          // remote transaction. Only a concrete destination outside the editor
          // means the user left the active semantic unit.
          if (relatedTarget instanceof HTMLElement && relatedTarget !== document.body && !view.dom.contains(relatedTarget)) {
            editorFocused.current = false;
            setEditingUnitId(null);
          }
          return false;
        },
        beforeinput: (view, event) => {
          const input = event as InputEvent;
          if (!isDocumentWideSelection(view) && !selectedUnitIds()) return false;
          if (input.inputType === "insertCompositionText") {
            composingDocumentReplacement.current = true;
            input.preventDefault();
            return true;
          }
          if (input.inputType === "insertText" && input.data != null) {
            input.preventDefault();
            return replaceUserSelection(view, input.data);
          }
          if (["deleteContentBackward", "deleteContentForward", "deleteByCut"].includes(input.inputType)) {
            input.preventDefault();
            return replaceUserSelection(view, "");
          }
          return false;
        },
        compositionstart: () => { composing.current = true; return false; },
        compositionend: (view, event) => {
          composing.current = false;
          if (composingDocumentReplacement.current) {
            composingDocumentReplacement.current = false;
            const text = (event as CompositionEvent).data;
            if (text) replaceUserSelection(view, text);
          }
          queueSave();
          return false;
        },
      },
    },
    onSelectionUpdate: ({ editor: current }) => {
      let unitId: string | null = null;
      for (let depth = current.state.selection.$from.depth; depth >= 0; depth--) {
        const id = current.state.selection.$from.node(depth).attrs.semanticUnitId;
        if (typeof id === "string") { unitId = id; break; }
      }
      setEditingUnitId((previous) => previous === unitId ? previous : unitId);
    },
  });
  function persistDrafts() {
    try { localStorage.setItem(draftKey(snapshot.session_id), JSON.stringify(draftRef.current)); }
    catch { setSaveState("端末への下書き保存に失敗しました"); }
    setDraftVersion((n) => n + 1);
  }
  function capture(current: NonNullable<ReturnType<typeof useEditor>>) {
    const map = ranges(current);
    const next = { ...draftRef.current };
    for (const [id, range] of map) {
      const unit = unitRef.current.find((item) => item.id === id);
      if (!unit) continue;
      const value = unitMarkdown(current, range.nodes);
      if (value.trim() === unit.markdown.trim()) delete next[id];
      else next[id] = { markdown: value, baseRevision: next[id]?.baseRevision ?? unit.revision, requestId: newRequestId() };
    }
    draftRef.current = next;
    persistDrafts();
    setSaveState(Object.keys(next).length ? "未保存の変更" : "保存済み");
    queueSave();
  }
  function queueSave() {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { if (!composing.current) void saveRef.current(false); }, 650);
  }
  const savePending = useCallback(async (force = false) => {
    if (!editor || inFlight.current || composing.current) return;
    const pending = Object.entries(draftRef.current);
    if (!pending.length && !structuralDeletes.current.size) return;
    inFlight.current = true; setSaveState("保存中…");
    let failed = false, blocked = false;
    try {
      {
        const operations: Record<string, unknown>[] = [];
        const savedDraftIds: string[] = [];
        const requestId = pending[0]?.[1].requestId ?? newRequestId();
        for (const [id, draft] of pending) {
          const unit = unitRef.current.find((item) => item.id === id);
          if (!unit) continue;
          if (conflicted.includes(id) && !force) { blocked = true; continue; }
          savedDraftIds.push(id);
          if (unit.type === "document_title") operations.push({ op: "update_document_title", title: draft.markdown.replace(/^#{1,6}\s*/, "").trim(), expected_revision: conflicted.includes(id) ? unit.revision : draft.baseRevision });
          else if (unit.type === "document_takeaway") operations.push({ op: "update_document_takeaway", takeaway: draft.markdown.replace(/^##\s*要点\s*\n?/, "").trim(), expected_revision: conflicted.includes(id) ? unit.revision : draft.baseRevision });
          else if (unit.type === "topic") operations.push({ op: "update_topic", id, expected_revision: conflicted.includes(id) ? unit.revision : draft.baseRevision, title: draft.markdown.replace(/^#{1,6}\s*/, "").trim() });
          else operations.push({ op: "update_block", id, expected_revision: conflicted.includes(id) ? unit.revision : draft.baseRevision, markdown: draft.markdown, kind: unit.kind ?? "point" });
        }
        for (const id of structuralDeletes.current) {
          const unit = unitRef.current.find((item) => item.id === id);
          if (unit && (unit.type === "topic" || unit.type === "block")) operations.push({ op: unit.type === "topic" ? "delete_topic" : "delete_block", id, expected_revision: unit.revision });
        }
        if (operations.length) await controller.mutate("/document/edits", "POST", { expected_note_revision: snapshot.note_revision, request_id: requestId, operations });
        for (const id of savedDraftIds) {
          const draft = pending.find(([pendingId]) => pendingId === id)?.[1];
          const unit = unitRef.current.find((item) => item.id === id);
          if (draft && draftRef.current[id]?.requestId === draft.requestId) delete draftRef.current[id];
          else if (draft && unit && draftRef.current[id]) draftRef.current[id] = { ...draftRef.current[id], baseRevision: unit.revision + 1 };
        }
        structuralDeletes.current.clear();
        localStorage.removeItem(structuralDraftKey(snapshot.session_id));
        persistDrafts();
        setConflicted([]); setSaveState(Object.keys(draftRef.current).length ? "保存中…" : "保存済み"); onError(null);
      }
      setConflicted([]); setSaveState(Object.keys(draftRef.current).length ? "保存中…" : "保存済み"); onError(null);
    } catch (cause) {
      failed = true;
      setConflicted((previous) => [...new Set([...previous, ...pending.map(([id]) => id), ...structuralDeletes.current])]);
      const message = cause instanceof Error ? cause.message : "保存に失敗しました";
      setSaveState("競合または接続エラー · 入力を保持中"); onError(message);
      await controller.refresh();
    } finally { inFlight.current = false; if (!failed && !blocked && Object.keys(draftRef.current).length) queueSave(); }
  }, [controller, editor, snapshot, conflicted, editingUnitId, onError]);
  saveRef.current = savePending;

  // Reconcile one server semantic unit at a time. A dirty unit is never replaced.
  useEffect(() => {
    if (!editor) return;
    if (Object.keys(draftRef.current).length) queueSave();
    // Existing editor state at mount is an untagged empty starter paragraph. Replace it
    // once with the server's identified document; later updates stay node-targeted.
    const current = ranges(editor);
    const visibleUnits = units.filter((unit) => !structuralDeletes.current.has(unit.id));
    const replacements = visibleUnits.flatMap((unit) => {
      if (draftRef.current[unit.id]) {
        const draft = draftRef.current[unit.id];
        if (unit.revision !== draft.baseRevision) setConflicted((list) => list.includes(unit.id) ? list : [...list, unit.id]);
        return [];
      }
      const existing = current.get(unit.id);
      const text = existing ? unitMarkdown(editor, existing.nodes) : "";
      if (existing && text.trim() === unit.markdown.trim()) return [];
      return [{ unit, existing }];
    });
    const currentIds = [...current.keys()];
    const remove = currentIds.filter((id) => !visibleUnits.some((unit) => unit.id === id) && !draftRef.current[id]).map((id) => ({ id, range: current.get(id)! }));
    const focusedInEditor = editorFocused.current || editor.view.hasFocus();
    const selectionAnchor = editor.state.selection.anchor;
    const selectionUnitId = [...current.entries()].find(([, range]) => selectionAnchor > range.from && selectionAnchor < range.to)?.[0] ?? null;
    const nativeSelection = window.getSelection();
    const nativeAnchor = nativeSelection?.anchorNode;
    const nativeElement = nativeAnchor instanceof Element ? nativeAnchor : nativeAnchor?.parentElement;
    const nativeUnit = nativeElement && editor.view.dom.contains(nativeElement) ? nativeElement.closest<HTMLElement>("[data-semantic-unit-id]") : null;
    // The browser's caret can be ahead of ProseMirror's selection during an
    // End key event. Protect that actual DOM unit before mapping a remote edit.
    const protectedId = focusedInEditor ? nativeUnit?.dataset.semanticUnitId ?? selectionUnitId ?? editingUnitId : null;
    if (!replacements.length && !remove.length && (currentIds.length > 0 || visibleUnits.length === 0)) return;
    const view = editor.view;
    const hadFocus = focusedInEditor;
    const scrollElement = document.querySelector<HTMLElement>(".seam-document-scroll");
    const previousScrollTop = scrollElement?.scrollTop;
    const visibleAnchor = scrollElement ? [...scrollElement.querySelectorAll<HTMLElement>("[data-semantic-unit-id]")].find((element) => element.getBoundingClientRect().bottom > scrollElement.getBoundingClientRect().top) : null;
    const anchorId = visibleAnchor?.dataset.semanticUnitId;
    const anchorTop = visibleAnchor?.getBoundingClientRect().top;
    const anchorRangeBefore = anchorId ? current.get(anchorId) : undefined;
    let anchorMoved = false;
    const restoreScroll = () => {
      if (scrollElement && !anchorMoved && previousScrollTop !== undefined) {
        // A change below the visible anchor must not cause a scroll correction.
        // Layout can settle after dispatch; using its transient bounding box
        // would otherwise scroll the document by a few dozen pixels.
        scrollElement.scrollTop = previousScrollTop;
      } else if (scrollElement && anchorId && anchorTop !== undefined) {
        const anchorAfter = scrollElement.querySelector<HTMLElement>(`[data-semantic-unit-id="${CSS.escape(anchorId)}"]`);
        if (anchorAfter) scrollElement.scrollTop += anchorAfter.getBoundingClientRect().top - anchorTop;
      } else if (scrollElement && previousScrollTop !== undefined) scrollElement.scrollTop = previousScrollTop;
    };
    const protectedRange = protectedId ? current.get(protectedId) : undefined;
    const protectedSelection = protectedRange && focusedInEditor ? {
      anchor: editor.state.selection.anchor - protectedRange.from - 1,
      head: editor.state.selection.head - protectedRange.from - 1,
    } : null;
    const nativeBookmark = nativeSelection?.anchorNode && nativeSelection.focusNode ? {
      anchorNode: nativeSelection.anchorNode,
      anchorOffset: nativeSelection.anchorOffset,
      focusNode: nativeSelection.focusNode,
      focusOffset: nativeSelection.focusOffset,
    } : null;
    let tr = editor.state.tr;
    const bootstrap = currentIds.length === 0 && visibleUnits.length > 0;
    if (bootstrap) tr = tr.replaceWith(0, tr.doc.content.size, visibleUnits.flatMap((unit) => nodesForMarkdown(editor, { ...unit, markdown: draftRef.current[unit.id]?.markdown ?? unit.markdown })));
    else {
      const mappedActions = [
        ...remove.filter(({ id }) => id !== protectedId).map(({ range }) => ({ from: range.from, to: range.to, type: "delete" as const })),
        ...replacements.flatMap(({ unit, existing }) => existing && unit.id !== protectedId ? [{ from: existing.from, to: existing.to, type: "replace" as const, unit }] : []),
      ].sort((a, b) => b.from - a.from);
      for (const action of mappedActions) {
        if (action.type === "delete") tr = tr.delete(action.from, action.to);
        else tr = tr.replaceWith(action.from, action.to, nodesForMarkdown(editor, action.unit));
      }
      for (const { unit } of replacements.filter(({ existing }) => !existing).sort((a, b) => a.unit.position - b.unit.position)) {
        tr = tr.insert(tr.doc.content.size, nodesForMarkdown(editor, unit));
      }
    }
    if (visibleUnits.length === 0 && tr.doc.childCount === 0) tr = tr.insert(0, editor.schema.nodes.paragraph.create());
    if (tr.docChanged) {
      anchorMoved = !!anchorRangeBefore && tr.mapping.map(anchorRangeBefore.from) !== anchorRangeBefore.from;
      let selection = editor.state.selection.map(tr.doc, tr.mapping);
      if (protectedSelection && protectedId) {
        let unitRange: { from: number; to: number } | null = null;
        tr.doc.forEach((node, offset) => {
          if (node.attrs.semanticUnitId === protectedId) unitRange = { from: unitRange?.from ?? offset, to: offset + node.nodeSize };
        });
        if (unitRange) {
          const range = unitRange as { from: number; to: number };
          const anchor = Math.max(range.from + 1, Math.min(range.to - 1, range.from + 1 + protectedSelection.anchor));
          const head = Math.max(range.from + 1, Math.min(range.to - 1, range.from + 1 + protectedSelection.head));
          try { selection = TextSelection.create(tr.doc, anchor, head); } catch {}
        }
      }
      tr.setSelection(selection);
      view.dispatch(tr.setMeta("addToHistory", false).setMeta("seamRemote", true));
      if (hadFocus) {
        const restoreFocusAndSelection = () => {
          // ProseMirror's focus() can scroll the selection into view after our
          // anchor restoration. Focus the editable DOM without browser scroll;
          // the transaction and bookmark already restored its selection.
          if (editorFocused.current) view.dom.focus({ preventScroll: true });
          if (nativeBookmark && nativeBookmark.anchorNode.isConnected && nativeBookmark.focusNode.isConnected && view.dom.contains(nativeBookmark.anchorNode) && view.dom.contains(nativeBookmark.focusNode)) {
            window.getSelection()?.setBaseAndExtent(nativeBookmark.anchorNode, nativeBookmark.anchorOffset, nativeBookmark.focusNode, nativeBookmark.focusOffset);
          }
        };
        restoreFocusAndSelection();
        restoreScroll();
        requestAnimationFrame(() => requestAnimationFrame(() => {
          if (editorFocused.current && nativeBookmark && nativeBookmark.anchorNode.isConnected && nativeBookmark.focusNode.isConnected && view.dom.contains(nativeBookmark.anchorNode) && view.dom.contains(nativeBookmark.focusNode)) {
            window.getSelection()?.setBaseAndExtent(nativeBookmark.anchorNode, nativeBookmark.anchorOffset, nativeBookmark.focusNode, nativeBookmark.focusOffset);
          }
          restoreScroll();
        }));
      }
    }
  }, [editor, units, editingUnitId]);

  useEffect(() => {
    if (!editor) return;
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (visible) setActive((visible.target as HTMLElement).dataset.semanticUnitId ?? null);
    }, { root: document.querySelector(".seam-document-scroll"), rootMargin: "-24px 0px -70% 0px" });
    document.querySelectorAll(".seam-document-editor [data-semantic-unit-id]").forEach((section) => observer.observe(section));
    return () => observer.disconnect();
  }, [editor, units]);
  useEffect(() => () => { if (timer.current !== null) window.clearTimeout(timer.current); }, []);

  const topics = snapshot.topics.filter((topic) => !(snapshot.document?.takeaway && /^(会議の要点|要点|概要|summary)$/i.test(topic.title.trim()))).slice().sort((a, b) => a.position - b.position);
  const structureAction = async (path: string, method: string, body: Record<string, unknown>) => {
    try { await controller.mutate(path, method, body); onError(null); return true; }
    catch (cause) { onError(cause instanceof Error ? cause.message : "構成の変更を保存できませんでした"); return false; }
  };
  const removeUnit = async (path: string, revision: number, label: string) => {
    if (!window.confirm(`${label}を削除しますか？更新履歴から元に戻せます。`)) return;
    await structureAction(path, "DELETE", { expected_revision: revision, request_id: newRequestId() });
  };
  const reorderTopic = (index: number, offset: number) => {
    const ids = topics.map((topic) => topic.id);
    [ids[index], ids[index + offset]] = [ids[index + offset], ids[index]];
    void structureAction("/topics/reorder", "POST", { topic_ids: ids, expected_revision: snapshot.note_revision, request_id: newRequestId() });
  };
  const pendingSuggestions = snapshot.suggestions.filter((item) => (!item.status || item.status === "pending") && !resolvedSuggestions[item.id]);
  const resolveSuggestion = async (suggestion: LiveSnapshot["suggestions"][number], action: "accept" | "dismiss") => {
    setSuggestionBusy(suggestion.id);
    setSuggestionFeedback("提案を処理しています…");
    try {
      await controller.mutate(`/suggestions/${encodeURIComponent(suggestion.id)}/resolve`, "POST", { action, expected_revision: snapshot.note_revision, request_id: newRequestId() });
      setResolvedSuggestions((items) => ({ ...items, [suggestion.id]: action === "accept" ? "apply" : "keep" }));
      setSuggestionFeedback(action === "accept" ? "提案を反映しました。" : "提案を見送りました。");
      onError(null);
    } catch (cause) {
      const stale = cause instanceof LiveApiError && cause.status === 409;
      setSuggestionFeedback(stale ? "ノートが更新されました。提案を確認し直してから操作してください。" : cause instanceof Error ? `操作できませんでした: ${cause.message}` : "操作できませんでした。もう一度お試しください。");
      onError(stale ? "ノートが更新されたため、提案の操作を保留しました。内容を確認し直してください。" : cause instanceof Error ? cause.message : "提案を保存できませんでした");
    } finally { setSuggestionBusy(null); }
  };
  return <div className="seam-live-document-layout">
    <aside className="seam-live-outline" aria-label="議論の目次">
      <div className="seam-outline-heading"><span>議論の目次</span><small>{topics.length}項目</small></div>
      <nav>{topics.map((topic) => <button key={topic.id} type="button" className={topic.id === active ? "is-active" : ""} onClick={() => { const section = document.querySelector(`[data-semantic-unit-id="${CSS.escape(topic.id)}"]`); section?.scrollIntoView({ behavior: "smooth", block: "start" }); }}><span>{topic.title}</span>{topic.id === snapshot.active_topic_id && <i aria-label="現在の話題" />}</button>)}</nav>
      <p>前の話題も残り、関連する発言が出ると同じ場所が更新されます。</p>
    </aside>
    <main className="seam-document-scroll">
      <div className="seam-document-inner">
        <div className="seam-document-heading"><p>{snapshot.processing ? "議論を整理しています" : "会議中に編集できます"}</p><span className="seam-save-state" role="status" aria-live="polite">{saveState}{draftVersion > 0 && conflicted.length > 0 ? ` · ${conflicted.length}件の競合` : ""}</span></div>
        {conflicted.length > 0 && <div className="seam-document-conflict" role="status"><span>サーバー側もこの範囲を更新しました。あなたの入力は端末に保持しています。</span><button type="button" onClick={() => { setConflicted([]); void savePending(true); }}>自分の入力を保存</button></div>}
        {suggestionFeedback && <p className="seam-suggestion-feedback" role="status" aria-live="polite">{suggestionFeedback}</p>}
        {pendingSuggestions.length > 0 && <details className="seam-suggestion-review">
          <summary>確認が必要 {pendingSuggestions.length}件</summary>
          <div className="seam-suggestion-list">{pendingSuggestions.map((suggestion) => {
            const review = getLiveSuggestionReview(suggestion, snapshot);
            const busy = suggestionBusy === suggestion.id;
            const disabled = suggestionBusy !== null;
            return <article className="seam-document-suggestion" key={suggestion.id}>
              <h2>{review.summary}</h2>
              <p className="seam-suggestion-target">対象: {review.targetTitle}</p>
              <p className="seam-suggestion-reason">{review.reason}</p>
              <div className="seam-suggestion-comparison">
                <section><h3>現在のMarkdown</h3><pre>{review.currentMarkdown}</pre></section>
                <section><h3>提案されたMarkdown</h3><pre>{review.proposedMarkdown}</pre></section>
              </div>
              {review.sources.length > 0 && <div className="seam-suggestion-sources"><h3>関連する発話</h3>{review.sources.map((source, index) => <blockquote key={`${source.label}-${index}`}><strong>{source.label}</strong>{source.text && <p>{source.text}</p>}</blockquote>)}</div>}
              <div className="seam-suggestion-actions">
                <button type="button" disabled={disabled} onClick={() => void resolveSuggestion(suggestion, "accept")}>{busy ? "処理中…" : "提案を反映"}</button>
                <button type="button" disabled={disabled} onClick={() => void resolveSuggestion(suggestion, "dismiss")}>{busy ? "処理中…" : "現在の内容を保つ"}</button>
              </div>
            </article>;
          })}</div>
        </details>}
        <EditorContent editor={editor} />
        <details className="seam-structure-panel"><summary>論点とメモを追加・整理</summary>
          <form className="seam-structure-add-topic" onSubmit={(event) => { event.preventDefault(); const title = topicDraft.text.trim(); if (title) void structureAction("/topics", "POST", { title, request_id: topicDraft.request_id }).then((saved) => { if (saved) topicDraft.clear(); }); }}>
            <label className="sr-only" htmlFor={`v2-new-topic-${snapshot.session_id}`}>新しい論点</label><input id={`v2-new-topic-${snapshot.session_id}`} value={topicDraft.text} onChange={(event) => topicDraft.update(event.target.value)} placeholder="新しい論点の見出し" maxLength={120} />
            <button type="submit" disabled={!topicDraft.text.trim()}>論点を追加</button>
          </form>
          {topicDraft.storageError && <p role="alert">新しい論点を端末に保存できませんでした。</p>}
          {topics.map((topic, index) => <section className="seam-structure-topic" key={topic.id}>
            <header><strong>{topic.title}</strong><span>{topic.blocks.length}件のメモ</span><button type="button" disabled={index === 0} aria-label={`${topic.title}を上へ移動`} onClick={() => reorderTopic(index, -1)}>↑</button><button type="button" disabled={index === topics.length - 1} aria-label={`${topic.title}を下へ移動`} onClick={() => reorderTopic(index, 1)}>↓</button><button type="button" onClick={() => void removeUnit(`/topics/${encodeURIComponent(topic.id)}`, topic.revision, `「${topic.title}」`)}>論点を削除</button></header>
            <NewBlockForm sessionId={snapshot.session_id} topic={topic} controller={controller} onError={onError} />
            {topic.blocks.slice().sort((a, b) => a.position - b.position).map((block) => <div className="seam-structure-block" key={block.id}><span>{block.content_md.split("\n")[0] || "空のメモ"}</span><small>{block.owner === "human" ? "手動" : "AI"}{block.source_segment_seqs?.length ? ` · 発話 ${block.source_segment_seqs.join(", ")}` : ""}</small><button type="button" onClick={() => void removeUnit(`/blocks/${encodeURIComponent(block.id)}`, block.revision, "このメモ")}>メモを削除</button></div>)}
          </section>)}
          {topics.length === 0 && <p>最初の論点を追加すると、会議ノートを整理できます。</p>}
        </details>
        {units.some((unit) => unit.source_segments?.length || unit.semantic_role || unit.epistemic_state) && <details className="seam-evidence-panel"><summary>根拠と内容の状態</summary>{units.filter((unit) => unit.source_segments?.length || unit.semantic_role || unit.epistemic_state).map((unit) => { const block = snapshot.topics.flatMap((topic) => topic.blocks).find((item) => item.id === unit.id); return <div key={unit.id}><strong>{unit.type === "topic" ? snapshot.topics.find((topic) => topic.id === unit.id)?.title : block?.content_md.split("\n")[0] || unit.markdown.split("\n")[0]}</strong><span>{unit.semantic_role || ""}{unit.epistemic_state ? ` · ${unit.epistemic_state}` : ""}</span>{unit.source_segments?.length ? <small>根拠の発話: {unit.source_segments.join(", ")}</small> : null}</div>; })}</details>}
        {topics.length === 0 && <div className="seam-document-empty">確定した発話を、意味ごとに整理したノートがここに表示されます。</div>}
      </div>
    </main>
  </div>;
}

function NewBlockForm({ sessionId, topic, controller, onError }: { sessionId: string; topic: LiveTopic; controller: LiveNotesController; onError: (message: string | null) => void }) {
  const draft = useNewNoteDraft(`seam:live-new-block:v1:${sessionId}:${topic.id}`);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); const content = draft.text.trim(); if (!content) return;
    try { await controller.mutate("/blocks", "POST", { topic_id: topic.id, content_md: content, kind: "point", request_id: draft.request_id }); draft.clear(); onError(null); }
    catch (cause) { onError(cause instanceof Error ? cause.message : "メモを保存できませんでした"); }
  };
  return <form className="seam-structure-add-block" onSubmit={(event) => { void submit(event); }}><label className="sr-only" htmlFor={`v2-new-block-${topic.id}`}>「{topic.title}」にメモを追加</label><textarea id={`v2-new-block-${topic.id}`} value={draft.text} onChange={(event) => draft.update(event.target.value)} placeholder="この論点にメモを追加（Markdown）" rows={2} /><button type="submit" disabled={!draft.text.trim()}>メモを追加</button>{draft.storageError && <small role="alert">メモの下書きを端末に保存できませんでした。</small>}</form>;
}
