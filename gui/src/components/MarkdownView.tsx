/* MarkdownEditor の読み取り専用版。
   編集モードと同じ ProseMirror スキーマで描画するので、見出し / 箇条書き /
   リンク / コードブロックなどの見た目が編集モードと完全に揃う。
   react-markdown と二重メンテだったスタイル不整合を解消する目的。 */
import { useEditor, EditorContent, Extension } from "@tiptap/react";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import TaskList from "@tiptap/extension-task-list";
import TaskItem from "@tiptap/extension-task-item";
import { Markdown } from "tiptap-markdown";
import { useEffect, useRef } from "react";

interface Props {
  value: string;
  className?: string;
  /** 検索ハイライト用クエリ (空なら無効) */
  searchQuery?: string;
  /** 現在フォーカス中のマッチ (0-based、範囲外なら全部「非アクティブ」表示) */
  activeMatchIndex?: number;
  /** マッチ件数が変わったときに呼ばれる (要約タブのカウンタ更新用) */
  onMatchesChange?: (count: number) => void;
}

const searchHighlightKey = new PluginKey("searchHighlight");

interface HighlightState {
  query: string;
  activeIndex: number;
  count: number;
  onCount: ((n: number) => void) | null;
}

function createSearchHighlight(stateRef: { current: HighlightState }) {
  return Extension.create({
    name: "searchHighlight",
    addProseMirrorPlugins() {
      return [
        new Plugin({
          key: searchHighlightKey,
          props: {
            decorations(editorState) {
              const { query, activeIndex } = stateRef.current;
              const q = (query || "").trim();
              if (!q) {
                if (stateRef.current.count !== 0) {
                  stateRef.current.count = 0;
                  queueMicrotask(() => stateRef.current.onCount?.(0));
                }
                return DecorationSet.empty;
              }
              const lcq = q.toLowerCase();
              const decos: Decoration[] = [];
              let matchIdx = 0;
              editorState.doc.descendants((node, pos) => {
                if (!node.isText) return;
                const text = node.text || "";
                const lct = text.toLowerCase();
                let p = 0;
                while (p < lct.length) {
                  const f = lct.indexOf(lcq, p);
                  if (f < 0) break;
                  const from = pos + f;
                  const to = pos + f + q.length;
                  const isActive = matchIdx === activeIndex;
                  decos.push(
                    Decoration.inline(from, to, {
                      class: isActive ? "search-mark search-mark-active" : "search-mark",
                      "data-mark-index": String(matchIdx),
                      ...(isActive ? { id: "detail-find-active-summary" } : {}),
                    }),
                  );
                  matchIdx++;
                  p = f + q.length;
                }
              });
              if (matchIdx !== stateRef.current.count) {
                stateRef.current.count = matchIdx;
                queueMicrotask(() => stateRef.current.onCount?.(matchIdx));
              }
              return DecorationSet.create(editorState.doc, decos);
            },
          },
        }),
      ];
    },
  });
}

export function MarkdownView({
  value, className, searchQuery, activeMatchIndex, onMatchesChange,
}: Props) {
  const highlightStateRef = useRef<HighlightState>({
    query: searchQuery || "",
    activeIndex: activeMatchIndex ?? -1,
    count: 0,
    onCount: onMatchesChange || null,
  });
  // refs を最新値に追従させる (state を毎回作り直さず、プラグイン側から読む)
  highlightStateRef.current.query = searchQuery || "";
  highlightStateRef.current.activeIndex = activeMatchIndex ?? -1;
  highlightStateRef.current.onCount = onMatchesChange || null;

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
      createSearchHighlight(highlightStateRef),
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

  // searchQuery / activeMatchIndex が変わったら decorations を再計算 (空 tr を dispatch)
  useEffect(() => {
    if (!editor) return;
    editor.view.dispatch(editor.state.tr);
    // 次フレームでアクティブマークを画面中央へ
    const id = requestAnimationFrame(() => {
      const el = document.getElementById("detail-find-active-summary");
      if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    return () => cancelAnimationFrame(id);
  }, [editor, searchQuery, activeMatchIndex]);

  if (!editor) return null;
  return (
    <div className={className}>
      <EditorContent editor={editor} />
    </div>
  );
}
