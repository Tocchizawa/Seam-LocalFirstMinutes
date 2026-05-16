/* TipTap ベースの markdown WYSIWYG エディタ。
   - 入出力は markdown 文字列。tiptap-markdown extension で双方向変換。
   - 表示用 .md-body と同じ typography を継承するように .ProseMirror を整形。
   - ツールバーは最小限 (H2/H3/Bold/Italic/Code/Bullet/Ordered/Quote/Link/Undo/Redo)。
*/
import { useEditor, EditorContent, type Editor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import TaskList from "@tiptap/extension-task-list";
import TaskItem from "@tiptap/extension-task-item";
import { Markdown } from "tiptap-markdown";
import {
  TextB, TextItalic, Code, ListBullets, ListNumbers, Quotes,
  Link as LinkIcon, ArrowCounterClockwise, ArrowClockwise,
  TextHOne, TextHTwo, TextHThree,
} from "@phosphor-icons/react";
import { useEffect } from "react";

interface Props {
  value: string;
  onChange: (markdown: string) => void;
  autoFocus?: boolean;
}

export function MarkdownEditor({ value, onChange, autoFocus }: Props) {
  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3, 4] },
      }),
      Link.configure({
        openOnClick: false,
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
    autofocus: autoFocus ? "end" : false,
    onUpdate: ({ editor }) => {
      const md = (editor.storage as { markdown?: { getMarkdown: () => string } })
        .markdown?.getMarkdown() ?? editor.getText();
      onChange(md);
    },
    editorProps: {
      attributes: {
        class: "md-body tiptap-content",
      },
    },
  });

  // 外部から value が刷新された場合 (生成完了など) に同期する。
  // ただしユーザー入力中の上書き暴発を防ぐため、現在の markdown と一致してれば触らない。
  useEffect(() => {
    if (!editor) return;
    const current = (editor.storage as { markdown?: { getMarkdown: () => string } })
      .markdown?.getMarkdown() ?? "";
    if (current.trim() !== value.trim()) {
      editor.commands.setContent(value, { emitUpdate: false });
    }
  }, [editor, value]);

  if (!editor) return null;

  return (
    <div className="flex flex-col h-full min-h-0">
      <Toolbar editor={editor} />
      <div className="flex-1 overflow-y-auto px-6 py-5">
        <EditorContent editor={editor} />
      </div>
    </div>
  );
}

function ToolbarButton({
  onClick, active, disabled, title, children,
}: {
  onClick: () => void;
  active?: boolean;
  disabled?: boolean;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-pressed={active}
      className={`tt-tool ${active ? "is-active" : ""}`}
    >
      {children}
    </button>
  );
}

function Divider() {
  return <span aria-hidden className="w-px h-4 bg-(--border) mx-0.5" />;
}

function Toolbar({ editor }: { editor: Editor }) {
  const can = editor.can();
  const promptLink = () => {
    const prev = editor.getAttributes("link").href || "";
    const url = window.prompt("リンク URL", prev);
    if (url === null) return;
    if (url === "") {
      editor.chain().focus().extendMarkRange("link").unsetLink().run();
      return;
    }
    editor.chain().focus().extendMarkRange("link").setLink({ href: url }).run();
  };

  return (
    <div className="tt-toolbar">
      <ToolbarButton
        onClick={() => editor.chain().focus().toggleHeading({ level: 1 }).run()}
        active={editor.isActive("heading", { level: 1 })}
        title="見出し 1"
      >
        <TextHOne size={14} weight="regular" />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}
        active={editor.isActive("heading", { level: 2 })}
        title="見出し 2"
      >
        <TextHTwo size={14} weight="regular" />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor.chain().focus().toggleHeading({ level: 3 }).run()}
        active={editor.isActive("heading", { level: 3 })}
        title="見出し 3"
      >
        <TextHThree size={14} weight="regular" />
      </ToolbarButton>
      <Divider />
      <ToolbarButton
        onClick={() => editor.chain().focus().toggleBold().run()}
        active={editor.isActive("bold")}
        title="太字 (⌘B)"
      >
        <TextB size={13} weight="bold" />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor.chain().focus().toggleItalic().run()}
        active={editor.isActive("italic")}
        title="斜体 (⌘I)"
      >
        <TextItalic size={13} weight="regular" />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor.chain().focus().toggleCode().run()}
        active={editor.isActive("code")}
        title="インラインコード (⌘E)"
      >
        <Code size={13} weight="regular" />
      </ToolbarButton>
      <Divider />
      <ToolbarButton
        onClick={() => editor.chain().focus().toggleBulletList().run()}
        active={editor.isActive("bulletList")}
        title="箇条書き"
      >
        <ListBullets size={14} weight="regular" />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor.chain().focus().toggleOrderedList().run()}
        active={editor.isActive("orderedList")}
        title="番号付きリスト"
      >
        <ListNumbers size={14} weight="regular" />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor.chain().focus().toggleBlockquote().run()}
        active={editor.isActive("blockquote")}
        title="引用"
      >
        <Quotes size={14} weight="regular" />
      </ToolbarButton>
      <ToolbarButton
        onClick={promptLink}
        active={editor.isActive("link")}
        title="リンク"
      >
        <LinkIcon size={13} weight="regular" />
      </ToolbarButton>
      <Divider />
      <ToolbarButton
        onClick={() => editor.chain().focus().undo().run()}
        disabled={!can.undo()}
        title="取り消し (⌘Z)"
      >
        <ArrowCounterClockwise size={13} weight="regular" />
      </ToolbarButton>
      <ToolbarButton
        onClick={() => editor.chain().focus().redo().run()}
        disabled={!can.redo()}
        title="やり直し (⌘⇧Z)"
      >
        <ArrowClockwise size={13} weight="regular" />
      </ToolbarButton>
    </div>
  );
}
