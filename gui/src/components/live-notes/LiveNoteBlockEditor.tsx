import { useEffect, useRef, useState } from "react";
import { useEditor, EditorContent } from "@tiptap/react";
import { DOMParser as ProseMirrorDOMParser } from "@tiptap/pm/model";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import TaskList from "@tiptap/extension-task-list";
import TaskItem from "@tiptap/extension-task-item";
import { Markdown } from "tiptap-markdown";
import { LIVE_KINDS, useDurableEdit, type LiveBlock, type LiveNotesController } from "../../lib/live-notes";

function markdown(editor: NonNullable<ReturnType<typeof useEditor>>) {
  return (editor.storage as { markdown?: { getMarkdown: () => string } }).markdown?.getMarkdown() ?? editor.getText();
}

export function LiveNoteBlockEditor({ sessionId, block, controller, onDelete }: {
  sessionId: string; block: LiveBlock; controller: LiveNotesController; onDelete: () => void;
}) {
  const edit = useDurableEdit({
    sessionId, entity: "block", id: block.id, revision: block.revision,
    value: { content_md: block.content_md, kind: block.kind },
    save: async (value, revision, requestId) => {
      const next = await controller.mutate(`/blocks/${encodeURIComponent(block.id)}`, "PATCH", {
        ...value, expected_revision: revision, request_id: requestId,
      });
      return next.topics.flatMap((topic) => topic.blocks).find((row) => row.id === block.id)?.revision ?? revision;
    },
  });
  const latest = useRef(edit); latest.current = edit;
  const [focused, setFocused] = useState(false);
  const editor = useEditor({
    extensions: [
      StarterKit.configure({ heading: { levels: [2, 3, 4] }, link: false }),
      Link.configure({ openOnClick: false, HTMLAttributes: { rel: "noopener noreferrer", target: "_blank" } }),
      TaskList, TaskItem.configure({ nested: true }),
      Markdown.configure({ html: false, linkify: true, transformPastedText: true }),
    ],
    content: edit.value.content_md,
    onUpdate: ({ editor: current, transaction }) => { if (!transaction.getMeta("liveNoteRemote")) latest.current.update({ ...latest.current.value, content_md: markdown(current) }); },
    onFocus: () => setFocused(true), onBlur: () => setFocused(false),
    editorProps: {
      attributes: { class: "md-body live-note-editor", role: "textbox", "aria-label": "議論メモ", "aria-multiline": "true" },
      handleDOMEvents: {
        compositionstart: () => { latest.current.compositionStart(); return false; },
        compositionend: () => { latest.current.compositionEnd(); return false; },
      },
    },
  });
  // Only this stable block's document changes, only when idle. Never reset the full note,
  // remount a focused editor, or apply a remote replacement over a durable local draft.
  useEffect(() => {
    if (!editor || focused || edit.dirty || editor.view.composing) return;
    if (markdown(editor) === block.content_md) return;
    const storage = editor.storage as { markdown?: { parser: { parse: (text: string) => string } } };
    const element = document.createElement("div");
    element.innerHTML = storage.markdown?.parser.parse(block.content_md) ?? "";
    const doc = ProseMirrorDOMParser.fromSchema(editor.schema).parse(element);
    editor.view.dispatch(editor.state.tr.replaceWith(0, editor.state.doc.content.size, doc.content).setMeta("addToHistory", false).setMeta("liveNoteRemote", true));
  }, [editor, block.content_md, focused, edit.dirty]);
  return (
    <article className={`live-note-block ${focused ? "is-editing" : ""}`} data-block-id={block.id} onMouseDown={(event) => { if ((event.target as HTMLElement).closest("button,summary")) event.preventDefault(); }}>
      <div className="live-note-block-meta">
        <label>
          <span className="sr-only">メモの種類</span>
          <select aria-label="メモの種類" value={edit.value.kind} onChange={(event) => edit.update({ ...edit.value, kind: event.target.value })}>
            {Object.entries(LIVE_KINDS).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}
          </select>
        </label>
        <span className="live-note-owner">{block.owner === "human" ? "手動編集 · AI上書き保護" : "AI"}</span>
        <span className="live-note-source" title="根拠の確定発話番号">{block.source_segment_seqs?.length ? `発話 ${block.source_segment_seqs.join(", ")}` : "手動追加"}</span>
        <button type="button" className="btn btn-ghost" aria-label="メモを削除" onClick={onDelete}>削除</button>
      </div>
      {focused && editor && <div className="live-note-inline-tools" onMouseDown={(event) => event.preventDefault()}>
        <button type="button" className="btn btn-ghost" aria-label="太字" onClick={() => editor.chain().focus().toggleBold().run()}>太字</button>
        <button type="button" className="btn btn-ghost" aria-label="箇条書き" onClick={() => editor.chain().focus().toggleBulletList().run()}>箇条書き</button>
        <button type="button" className="btn btn-ghost" aria-label="入力を取り消す" onClick={() => editor.chain().focus().undo().run()}>元に戻す</button>
      </div>}
      <EditorContent editor={editor} />
      <div className="live-note-save-state" aria-live="polite">
        {edit.storageError ? <span role="alert">端末への下書き保存に失敗しました。この画面を閉じずに保存を再試行してください。</span> : <span>{edit.status || (edit.dirty ? "復元した未保存の下書きがあります" : "")}</span>}
        {edit.dirty && <button type="button" className="btn btn-ghost" onClick={() => { void edit.retry(); }}>{edit.conflict ? "入力を最新の版へ再保存" : "保存を再試行"}</button>}
      </div>
      {edit.conflict && <details className="live-note-conflict"><summary>サーバーの最新内容を確認（入力は保持されています）</summary><p className="whitespace-pre-wrap">{block.content_md}</p></details>}
    </article>
  );
}
