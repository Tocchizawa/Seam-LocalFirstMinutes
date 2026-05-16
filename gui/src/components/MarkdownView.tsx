/* MarkdownEditor の読み取り専用版。
   編集モードと同じ ProseMirror スキーマで描画するので、見出し / 箇条書き /
   リンク / コードブロックなどの見た目が編集モードと完全に揃う。
   react-markdown と二重メンテだったスタイル不整合を解消する目的。 */
import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import TaskList from "@tiptap/extension-task-list";
import TaskItem from "@tiptap/extension-task-item";
import { Markdown } from "tiptap-markdown";
import { useEffect } from "react";

interface Props {
  value: string;
  className?: string;
}

export function MarkdownView({ value, className }: Props) {
  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3, 4] },
      }),
      Link.configure({
        openOnClick: true,
        autolink: true,
        HTMLAttributes: { rel: "noopener noreferrer", target: "_blank" },
      }),
      TaskList,
      TaskItem.configure({ nested: true }),
      Markdown.configure({
        html: false,
        linkify: true,
        breaks: false,
        transformPastedText: true,
      }),
    ],
    content: value,
    editable: false,
    editorProps: {
      attributes: {
        class: "md-body tiptap-content tiptap-readonly",
      },
    },
  });

  // 値が外部から更新されたとき同期 (ストリーミング要約の途中経過などで頻繁に変わる)
  useEffect(() => {
    if (!editor) return;
    const cur = (editor.storage as { markdown?: { getMarkdown: () => string } })
      .markdown?.getMarkdown() ?? "";
    if (cur.trim() !== value.trim()) {
      editor.commands.setContent(value, { emitUpdate: false });
    }
  }, [editor, value]);

  if (!editor) return null;
  return (
    <div className={className}>
      <EditorContent editor={editor} />
    </div>
  );
}
